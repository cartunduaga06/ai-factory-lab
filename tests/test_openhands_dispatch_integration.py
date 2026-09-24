"""End-to-end seam test: DispatchService -> OpenHandsAdapter, offline.

This proves the full Phase 3 exit criterion without a live server: a real
``FactoryTask`` is claimed by the orchestrator, dispatched through the real
``OpenHandsAdapter``, persisted as a durable ``AgentRun``, and then collected
back to a terminal state. The only thing faked is the HTTP transport.
"""

from __future__ import annotations

import json
from pathlib import Path

from factory.domain.enums import AgentKind, RunStatus, TaskStatus
from factory.domain.models import AgentAdapter, FactoryTask, TaskSource
from factory.infrastructure.persistence import SqliteRunRepository, SqliteTaskRepository
from factory.integrations.openhands.adapter import OpenHandsAdapter
from factory.integrations.openhands.client import OpenHandsClient, ServerResponse
from factory.integrations.openhands.execution import OpenHandsExecution
from factory.orchestration import DispatchService
from tests.fake_openhands import FakeTransport
from tests.fake_workspace import FakeWorkspaceProvisioner

BASE_URL = "http://localhost:60000"
CONVERSATION_ID = "24e054d8-7e5d-4749-b9c3-ee020248f84b"


def _ready_task(tasks: SqliteTaskRepository) -> FactoryTask:
    task = tasks.save(
        FactoryTask(
            title="Inspect the workspace and summarize it",
            target_repository="cartunduaga06/finanza-ia",
            body="Do not modify files.",
            source=TaskSource("github", "cartunduaga06/ai-factory-lab", 7),
        )
    )
    tasks.apply_transition(task.task_id, TaskStatus.DISCOVERED, TaskStatus.READY)
    task.status = TaskStatus.READY
    return task


def test_dispatch_then_collect_through_the_real_adapter(tmp_path: Path) -> None:
    db_path = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(db_path)
    tasks.initialize()
    runs = SqliteRunRepository(db_path)
    runs.initialize()

    transport = FakeTransport(
        [
            ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"}),
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "finished"}),
            ServerResponse(200, {"response": "Workspace inspected; no files changed."}),
        ]
    )
    client = OpenHandsClient(BASE_URL, transport=transport)
    adapter = OpenHandsAdapter(client, OpenHandsExecution(agent_profile_id="profile-1"))
    assert isinstance(adapter, AgentAdapter)

    task = _ready_task(tasks)
    run = DispatchService(
        tasks, runs, provisioner=FakeWorkspaceProvisioner(), workspace_root=str(tmp_path / "ws")
    ).dispatch(task.task_id, adapter)

    # The factory owns the lifecycle: the task is RUNNING, and the run is durable.
    assert tasks.get(task.task_id).status is TaskStatus.RUNNING
    assert run.adapter is AgentKind.OPENHANDS
    assert run.run_id == CONVERSATION_ID
    assert runs.get_run(CONVERSATION_ID) is not None
    assert runs.get_workspace(run.workspace.workspace_id) is not None  # type: ignore[union-attr]

    # The dispatch request carried the factory workspace path and task text.
    request_body = json.loads(transport.requests[0].body.decode())
    assert request_body["workspace"]["working_dir"] == run.workspace.path  # type: ignore[union-attr]
    assert (
        "Inspect the workspace and summarize it"
        in (request_body["initial_message"]["content"][0]["text"])
    )

    # Collect normalizes the terminal engine state and captures a summary.
    collected = adapter.collect(run)
    assert collected.status is RunStatus.SUCCEEDED
    assert collected.finished_at is not None
    assert collected.summary == "Workspace inspected; no files changed."
