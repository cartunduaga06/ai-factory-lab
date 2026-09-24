"""Tests for the dispatch service.

Dispatch is exercised against :class:`tests.fake_adapter.FakeAgentAdapter`, a real
adapter implementation rather than a mock of the code under test, and against a
:class:`tests.fake_workspace.FakeWorkspaceProvisioner` that creates real
directories. No network and no real engine is involved. The *concrete* Git
worktree provisioner has its own tests in ``test_git_workspace.py``.
"""

from __future__ import annotations

import threading
import traceback
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, RunStatus, TaskStatus
from factory.domain.errors import (
    AgentDispatchError,
    DispatchConflictError,
    TaskNotReadyError,
    WorkspaceProvisioningError,
)
from factory.domain.models import AgentAdapter, AgentRun, FactoryTask, TaskSource
from factory.infrastructure.persistence import SqliteRunRepository, SqliteTaskRepository
from factory.orchestration import DispatchService
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_workspace import FakeWorkspaceProvisioner


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "factory.db")


def _tasks(db_path: str) -> SqliteTaskRepository:
    repository = SqliteTaskRepository(db_path)
    repository.initialize()
    return repository


def _runs(db_path: str) -> SqliteRunRepository:
    repository = SqliteRunRepository(db_path)
    repository.initialize()
    return repository


def _ready_task(tasks: SqliteTaskRepository, number: int = 1) -> FactoryTask:
    task = tasks.save(
        FactoryTask(
            title="Task",
            target_repository="cartunduaga06/finanza-ia",
            source=TaskSource("github", "cartunduaga06/ai-factory-lab", number),
        )
    )
    tasks.apply_transition(task.task_id, TaskStatus.DISCOVERED, TaskStatus.READY)
    task.status = TaskStatus.READY
    return task


def _service(
    db_path: str, tmp_path: Path, *, provisioner: FakeWorkspaceProvisioner | None = None
) -> DispatchService:
    return DispatchService(
        _tasks(db_path),
        _runs(db_path),
        provisioner=provisioner or FakeWorkspaceProvisioner(),
        workspace_root=str(tmp_path / "workspaces"),
    )


# -- the happy path --------------------------------------------------------


def test_dispatch_claims_task_and_persists_workspace_and_run(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    adapter = FakeAgentAdapter()

    run = _service(db_path, tmp_path).dispatch(task.task_id, adapter)

    # A successful dispatch leaves the task RUNNING, not merely CLAIMED.
    assert tasks.get(task.task_id).status is TaskStatus.RUNNING
    assert run.status is RunStatus.PENDING
    assert run.adapter is AgentKind.OTHER
    assert adapter.dispatched == [(task.task_id, run.workspace.workspace_id)]

    stored = _runs(db_path)
    assert stored.get_run(run.run_id) is not None
    assert stored.get_workspace(run.workspace.workspace_id) is not None


def test_dispatch_records_claim_then_running(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    _service(db_path, tmp_path).dispatch(task.task_id, FakeAgentAdapter())

    history = tasks.history(task.task_id)
    assert [(h.from_status, h.to_status) for h in history] == [
        (TaskStatus.DISCOVERED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
        (TaskStatus.CLAIMED, TaskStatus.RUNNING),
    ]


def test_workspace_is_isolated_and_per_run(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    run = _service(db_path, tmp_path).dispatch(task.task_id, FakeAgentAdapter())

    workspace = run.workspace
    assert workspace is not None
    # The branch and path are keyed by the workspace's own id, not the task id.
    assert workspace.branch == f"factory/{task.task_id}/{workspace.workspace_id}"
    assert workspace.path == str(tmp_path / "workspaces" / workspace.workspace_id)
    assert workspace.repository_slug == task.target_repository


def test_workspace_exists_before_adapter_dispatch(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    adapter = FakeAgentAdapter()

    run = _service(db_path, tmp_path).dispatch(task.task_id, adapter)

    # The physical workspace existed by the time the adapter was called, and the
    # adapter received exactly the factory-selected path.
    assert adapter.path_existed_at_dispatch == [True]
    assert adapter.received_paths == [run.workspace.path]  # type: ignore[union-attr]
    assert Path(run.workspace.path).is_dir()  # type: ignore[union-attr]


def test_adapter_only_needs_the_protocol(db_path: str, tmp_path: Path) -> None:
    adapter = FakeAgentAdapter()
    assert isinstance(adapter, AgentAdapter)

    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    _service(db_path, tmp_path).dispatch(task.task_id, adapter)


def test_dispatch_result_survives_reinstantiation(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    run = _service(db_path, tmp_path).dispatch(task.task_id, FakeAgentAdapter())

    reopened = _service(db_path, tmp_path)
    loaded = reopened._runs.get_run(run.run_id)  # noqa: SLF001 - durability check
    assert loaded is not None
    assert loaded.workspace == run.workspace
    assert reopened._tasks.get(task.task_id).status is TaskStatus.RUNNING  # noqa: SLF001


# -- rejections ------------------------------------------------------------


def test_missing_task_raises_key_error(db_path: str, tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        _service(db_path, tmp_path).dispatch("missing", FakeAgentAdapter())


@pytest.mark.parametrize(
    "status",
    [TaskStatus.DISCOVERED, TaskStatus.RUNNING, TaskStatus.DONE, TaskStatus.CANCELLED],
)
def test_non_ready_task_is_rejected(db_path: str, tmp_path: Path, status: TaskStatus) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(
        FactoryTask(
            title="Task",
            target_repository="cartunduaga06/finanza-ia",
            status=status,
        )
    )
    adapter = FakeAgentAdapter()
    provisioner = FakeWorkspaceProvisioner()

    with pytest.raises(TaskNotReadyError):
        _service(db_path, tmp_path, provisioner=provisioner).dispatch(task.task_id, adapter)

    # Nothing was written: no claim, no workspace, no run, no adapter call.
    assert tasks.get(task.task_id).status is status
    assert _runs(db_path).list_runs(task.task_id) == []
    assert adapter.dispatched == []
    assert provisioner.prepared == []


# -- idempotent retry ------------------------------------------------------


def test_repeat_dispatch_does_not_duplicate(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    service = _service(db_path, tmp_path)
    first = service.dispatch(task.task_id, FakeAgentAdapter())

    # The task is now RUNNING, so a retry is refused and keeps the original run.
    with pytest.raises(TaskNotReadyError):
        service.dispatch(task.task_id, FakeAgentAdapter())

    runs = _runs(db_path)
    assert [r.run_id for r in runs.list_runs(task.task_id)] == [first.run_id]
    assert len(tasks.history(task.task_id)) == 3


def test_idempotency_rule_is_discoverable_through_active_run(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    run = _service(db_path, tmp_path).dispatch(task.task_id, FakeAgentAdapter())

    active = _runs(db_path).find_active_run(task.task_id)
    assert active is not None
    assert active.run_id == run.run_id


# -- workspace provisioning failure ---------------------------------------


def test_provisioning_failure_never_calls_the_adapter(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    adapter = FakeAgentAdapter()
    boom = RuntimeError("git failed with token ghp_secret in remote url")
    provisioner = FakeWorkspaceProvisioner(fail_with=boom)

    with pytest.raises(WorkspaceProvisioningError) as caught:
        _service(db_path, tmp_path, provisioner=provisioner).dispatch(task.task_id, adapter)

    # No fake success: the adapter was never called and no run was recorded.
    assert adapter.dispatched == []
    assert _runs(db_path).list_runs(task.task_id) == []
    # The claim is not rolled back (no destructive history rewrite); the legal
    # recovery path is BLOCKED/CANCELLED.
    assert tasks.get(task.task_id).status is TaskStatus.CLAIMED

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    for leak in ("ghp_secret", "remote url", "git failed"):
        assert leak not in str(error)
        assert leak not in repr(error)
        assert leak not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None


# -- adapter failure -------------------------------------------------------


def test_adapter_failure_is_typed_and_leaves_no_active_run(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    boom = RuntimeError("engine exploded with token ghp_secret")

    with pytest.raises(AgentDispatchError) as caught:
        _service(db_path, tmp_path).dispatch(task.task_id, FakeAgentAdapter(fail_with=boom))

    # The failed attempt is durable and terminal; the claim is not rolled back
    # and the task never reached RUNNING.
    runs = _runs(db_path)
    recorded = runs.list_runs(task.task_id)
    assert len(recorded) == 1
    assert recorded[0].status is RunStatus.FAILED
    assert recorded[0].finished_at is not None
    assert runs.find_active_run(task.task_id) is None
    assert tasks.get(task.task_id).status is TaskStatus.CLAIMED

    error = caught.value
    assert "ghp_secret" not in str(error)
    assert "ghp_secret" not in repr(error)
    assert error.run_id == recorded[0].run_id
    assert error.__cause__ is None
    assert error.__context__ is None

    formatted = "".join(traceback.format_exception(error))
    assert "ghp_secret" not in formatted
    assert "engine exploded" not in formatted


def test_retry_after_failed_attempt_requires_a_ready_task(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    service = _service(db_path, tmp_path)
    with pytest.raises(AgentDispatchError):
        service.dispatch(task.task_id, FakeAgentAdapter(fail_with=RuntimeError("boom")))

    with pytest.raises(TaskNotReadyError):
        service.dispatch(task.task_id, FakeAgentAdapter())
    assert len(_runs(db_path).list_runs(task.task_id)) == 1


# -- concurrency -----------------------------------------------------------


def test_concurrent_dispatch_allows_exactly_one_claim(db_path: str, tmp_path: Path) -> None:
    """Two dispatchers race for one READY task; exactly one may win.

    Both contenders run on independent connections and are released together by a
    barrier, so the race is explicit rather than timing-dependent. The loser must
    be refused in a controlled way and leave no workspace or run behind.
    """
    tasks = _tasks(db_path)
    task = _ready_task(tasks)

    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def contend(*, start: threading.Barrier = barrier, results: list[object] = outcomes) -> None:
        service = _service(db_path, tmp_path)
        adapter = FakeAgentAdapter()
        start.wait()
        try:
            results.append(service.dispatch(task.task_id, adapter))
        except Exception as exc:  # noqa: BLE001 - classified by assertions below
            results.append(exc)

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    winners = [outcome for outcome in outcomes if isinstance(outcome, AgentRun)]
    # Exactly one dispatch wins. The loser is refused by the READY guard, either
    # as a lost claim race (DispatchConflictError) or, if the winner had already
    # advanced the task past CLAIMED, as a not-ready rejection. Both are the
    # controlled refusal; neither creates a second workspace or run.
    losers = [
        outcome
        for outcome in outcomes
        if isinstance(outcome, (DispatchConflictError, TaskNotReadyError))
    ]
    assert len(winners) == 1, outcomes
    assert len(losers) == 1, outcomes

    history = tasks.history(task.task_id)
    claim_edges = [h for h in history if h.to_status is TaskStatus.CLAIMED]
    assert len(claim_edges) == 1

    runs = _runs(db_path)
    stored_runs = runs.list_runs(task.task_id)
    assert len(stored_runs) == 1
    assert stored_runs[0].run_id == winners[0].run_id

    workspace = runs.get_workspace(winners[0].workspace.workspace_id)
    assert workspace is not None
    assert tasks.get(task.task_id).status is TaskStatus.RUNNING


# -- per-run isolation guardrail ------------------------------------------


def test_two_runs_for_one_task_get_different_workspaces(db_path: str, tmp_path: Path) -> None:
    """A retry must not reuse another run's workspace or branch."""
    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    first = _service(db_path, tmp_path).dispatch(task.task_id, FakeAgentAdapter())

    # Terminalise the first run and send the task back through the retry edge.
    runs = _runs(db_path)
    stored = runs.get_run(first.run_id)
    assert stored is not None
    stored.status = RunStatus.FAILED
    stored.finished_at = stored.started_at
    runs.update_run(stored)
    tasks.apply_transition(task.task_id, TaskStatus.RUNNING, TaskStatus.FAILED)
    tasks.apply_transition(task.task_id, TaskStatus.FAILED, TaskStatus.READY)

    second = _service(db_path, tmp_path).dispatch(task.task_id, FakeAgentAdapter())

    assert second.workspace.workspace_id != first.workspace.workspace_id  # type: ignore[union-attr]
    assert second.workspace.branch != first.workspace.branch  # type: ignore[union-attr]
    assert second.workspace.path != first.workspace.path  # type: ignore[union-attr]
