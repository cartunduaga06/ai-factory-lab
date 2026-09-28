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
    RetryNotAllowedError,
    TaskNotReadyError,
    TaskStateChangedError,
    WorkspaceProvisioningError,
)
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    FactoryTask,
    TaskSource,
    Workspace,
    new_workspace,
)
from factory.infrastructure.persistence import SqliteRunRepository, SqliteTaskRepository
from factory.orchestration import DispatchService
from factory.orchestration.retry import RetryService
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

    # The failed attempt is durable and terminal; recovery is recorded through
    # BLOCKED without rolling back the claim or reaching RUNNING.
    runs = _runs(db_path)
    recorded = runs.list_runs(task.task_id)
    assert len(recorded) == 1
    assert recorded[0].status is RunStatus.FAILED
    assert recorded[0].finished_at is not None
    assert runs.find_active_run(task.task_id) is None
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED

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

    # Both associations are durable, and each run keeps its own workspace: the
    # storage-level one-workspace-per-run guard never collapses them.
    stored_runs = {r.run_id: r for r in _runs(db_path).list_runs(task.task_id)}
    assert stored_runs[first.run_id].workspace == first.workspace
    assert stored_runs[second.run_id].workspace == second.workspace


def test_explicit_retry_preserves_failed_attempt_and_creates_new_workspace(
    db_path: str, tmp_path: Path
) -> None:
    tasks = _tasks(db_path)
    runs = _runs(db_path)
    task = _ready_task(tasks)
    service = _service(db_path, tmp_path)
    with pytest.raises(AgentDispatchError) as caught:
        service.dispatch(task.task_id, FakeAgentAdapter(fail_with=RuntimeError("boom")))
    failed = runs.get_run(caught.value.run_id)
    assert failed is not None and failed.workspace is not None
    assert failed.status is RunStatus.FAILED
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED
    assert [edge.to_status for edge in tasks.history(task.task_id)] == [
        TaskStatus.READY,
        TaskStatus.CLAIMED,
        TaskStatus.BLOCKED,
    ]
    assert RetryService(tasks, runs).retry(task.task_id).status is TaskStatus.READY
    assert runs.list_runs(task.task_id) == [failed]
    assert runs.find_active_run(task.task_id) is None
    with pytest.raises(RetryNotAllowedError, match="not BLOCKED"):
        RetryService(tasks, runs).retry(task.task_id)
    next_run = service.dispatch(task.task_id, FakeAgentAdapter())
    assert next_run.workspace is not None
    assert next_run.run_id != failed.run_id
    assert next_run.workspace.workspace_id != failed.workspace.workspace_id
    assert next_run.workspace.path != failed.workspace.path
    assert next_run.workspace.branch != failed.workspace.branch
    assert Path(failed.workspace.path).is_dir()
    assert runs.get_run(failed.run_id) == failed
    assert runs.get_workspace(failed.workspace.workspace_id) == failed.workspace
    assert len(runs.list_runs(task.task_id)) == 2
    assert runs.find_active_run(task.task_id) == next_run
    with pytest.raises(TaskNotReadyError):
        service.dispatch(task.task_id, FakeAgentAdapter())
    assert len(runs.list_runs(task.task_id)) == 2


@pytest.mark.parametrize("status", list(TaskStatus))
def test_retry_rejects_other_states_and_claims_without_history(
    db_path: str, status: TaskStatus
) -> None:
    tasks = _tasks(db_path)
    runs = _runs(db_path)
    task = tasks.save(FactoryTask(title="retry", target_repository="example/target", status=status))
    if status is TaskStatus.BLOCKED:
        assert RetryService(tasks, runs).retry(task.task_id).status is TaskStatus.READY
    else:
        message = "latest run FAILED" if status is TaskStatus.CLAIMED else "not BLOCKED"
        with pytest.raises(RetryNotAllowedError, match=message):
            RetryService(tasks, runs).retry(task.task_id)
        assert tasks.get(task.task_id).status is status
        assert tasks.history(task.task_id) == []
    assert runs.list_runs(task.task_id) == []


def test_retry_refuses_active_run(db_path: str) -> None:
    tasks = _tasks(db_path)
    runs = _runs(db_path)
    task = tasks.save(
        FactoryTask(title="blocked", target_repository="example/target", status=TaskStatus.BLOCKED)
    )
    active = runs.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER))
    with pytest.raises(RetryNotAllowedError, match="active run"):
        RetryService(tasks, runs).retry(task.task_id)
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED
    assert runs.get_run(active.run_id) == active
    assert tasks.history(task.task_id) == []


def test_dispatch_refuses_active_run_before_creating_workspace(
    db_path: str, tmp_path: Path
) -> None:
    tasks = _tasks(db_path)
    runs = _runs(db_path)
    task = _ready_task(tasks)
    active = runs.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER))
    provisioner = FakeWorkspaceProvisioner()
    with pytest.raises(DispatchConflictError):
        _service(db_path, tmp_path, provisioner=provisioner).dispatch(
            task.task_id, FakeAgentAdapter()
        )
    assert provisioner.prepared == []
    assert tasks.get(task.task_id).status is TaskStatus.READY
    assert runs.list_runs(task.task_id) == [active]


@pytest.mark.parametrize("legacy_claim", [False, True])
def test_concurrent_retry_records_one_recovery(db_path: str, legacy_claim: bool) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(
        FactoryTask(
            title="blocked",
            target_repository="example/target",
            status=TaskStatus.CLAIMED if legacy_claim else TaskStatus.BLOCKED,
        )
    )
    runs = _runs(db_path)
    if legacy_claim:
        runs.save_run(
            AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=RunStatus.FAILED)
        )
    before = runs.list_runs(task.task_id)
    barrier = threading.Barrier(2)
    outcomes: list[object] = []

    def contend() -> None:
        service = RetryService(_tasks(db_path), _runs(db_path))
        barrier.wait()
        try:
            outcomes.append(service.retry(task.task_id))
        except Exception as exc:  # noqa: BLE001 - classify concurrent refusal below
            outcomes.append(exc)

    threads = [threading.Thread(target=contend) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(outcome, FactoryTask) for outcome in outcomes) == 1
    assert (
        sum(
            isinstance(outcome, (RetryNotAllowedError, TaskStateChangedError))
            for outcome in outcomes
        )
        == 1
    )
    expected = [(TaskStatus.BLOCKED, TaskStatus.READY)]
    if legacy_claim:
        expected.insert(0, (TaskStatus.CLAIMED, TaskStatus.BLOCKED))
    assert [(edge.from_status, edge.to_status) for edge in tasks.history(task.task_id)] == expected
    assert tasks.get(task.task_id).status is TaskStatus.READY
    assert runs.list_runs(task.task_id) == before


def test_legacy_claim_retry_preserves_attempt_and_next_dispatch_is_isolated(
    db_path: str, tmp_path: Path
) -> None:
    tasks = _tasks(db_path)
    runs = _runs(db_path)
    task = _ready_task(tasks)
    service = _service(db_path, tmp_path)
    claimed = service.lifecycle.transition(task.task_id, TaskStatus.CLAIMED)
    workspace = new_workspace(claimed, str(tmp_path / "workspaces"))
    FakeWorkspaceProvisioner().prepare(claimed, workspace)
    marker = Path(workspace.path) / "previous-attempt.txt"
    marker.write_text("failed attempt content", encoding="utf-8")
    failed = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.FAILED,
            workspace=workspace,
        )
    )
    original_history = list(tasks.history(task.task_id))

    assert RetryService(tasks, runs).retry(task.task_id).status is TaskStatus.READY
    history = list(tasks.history(task.task_id))
    assert history[: len(original_history)] == original_history
    assert [(edge.from_status, edge.to_status) for edge in history[len(original_history) :]] == [
        (TaskStatus.CLAIMED, TaskStatus.BLOCKED),
        (TaskStatus.BLOCKED, TaskStatus.READY),
    ]
    assert runs.list_runs(task.task_id) == [failed]
    assert runs.find_active_run(task.task_id) is None
    assert runs.get_workspace(workspace.workspace_id) == workspace
    assert marker.read_text(encoding="utf-8") == "failed attempt content"

    next_run = service.dispatch(task.task_id, FakeAgentAdapter())
    assert next_run.workspace is not None
    assert next_run.run_id != failed.run_id
    assert next_run.workspace.workspace_id != workspace.workspace_id
    assert next_run.workspace.branch != workspace.branch
    assert next_run.workspace.path != workspace.path
    assert runs.get_run(failed.run_id) == failed
    assert runs.get_workspace(workspace.workspace_id) == workspace
    assert marker.read_text(encoding="utf-8") == "failed attempt content"
    assert runs.find_active_run(task.task_id) == next_run
    with pytest.raises(TaskNotReadyError):
        service.dispatch(task.task_id, FakeAgentAdapter())
    assert len(runs.list_runs(task.task_id)) == 2


@pytest.mark.parametrize(
    "latest_status",
    [RunStatus.PENDING, RunStatus.RUNNING, RunStatus.SUCCEEDED, RunStatus.CANCELLED],
)
def test_legacy_claim_refuses_latest_non_failed_run(db_path: str, latest_status: RunStatus) -> None:
    tasks = _tasks(db_path)
    runs = _runs(db_path)
    task = tasks.save(
        FactoryTask(title="legacy", target_repository="example/target", status=TaskStatus.CLAIMED)
    )
    runs.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=RunStatus.FAILED))
    runs.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=latest_status))
    before = runs.list_runs(task.task_id)
    message = (
        "active run"
        if latest_status in {RunStatus.PENDING, RunStatus.RUNNING}
        else ("latest run FAILED")
    )
    with pytest.raises(RetryNotAllowedError, match=message):
        RetryService(tasks, runs).retry(task.task_id)
    assert tasks.get(task.task_id).status is TaskStatus.CLAIMED
    assert tasks.history(task.task_id) == []
    assert runs.list_runs(task.task_id) == before


def test_legacy_claim_refuses_active_run_even_when_latest_is_failed(db_path: str) -> None:
    tasks = _tasks(db_path)
    runs = _runs(db_path)
    task = tasks.save(
        FactoryTask(title="legacy", target_repository="example/target", status=TaskStatus.CLAIMED)
    )
    active = runs.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER))
    runs.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=RunStatus.FAILED))
    before = runs.list_runs(task.task_id)
    with pytest.raises(RetryNotAllowedError, match="active run"):
        RetryService(tasks, runs).retry(task.task_id)
    assert tasks.get(task.task_id).status is TaskStatus.CLAIMED
    assert tasks.history(task.task_id) == []
    assert runs.list_runs(task.task_id) == before
    assert runs.find_active_run(task.task_id) == active


def test_legacy_claim_rechecks_exact_status_before_transition(db_path: str) -> None:
    class ChangedClaimRepository(SqliteTaskRepository):
        """Reproduce a concurrent cancellation after retry reads the original claim."""

        def get(self, task_id: str) -> FactoryTask | None:
            task = super().get(task_id)
            if task is not None and task.status is TaskStatus.CLAIMED:
                self.apply_transition(task_id, TaskStatus.CLAIMED, TaskStatus.CANCELLED)
            return task

    tasks = _tasks(db_path)
    runs = _runs(db_path)
    task = tasks.save(
        FactoryTask(title="legacy", target_repository="example/target", status=TaskStatus.CLAIMED)
    )
    failed = runs.save_run(
        AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=RunStatus.FAILED)
    )
    with pytest.raises(TaskStateChangedError):
        RetryService(ChangedClaimRepository(db_path), runs).retry(task.task_id)
    assert tasks.get(task.task_id).status is TaskStatus.CANCELLED
    assert [(edge.from_status, edge.to_status) for edge in tasks.history(task.task_id)] == [
        (TaskStatus.CLAIMED, TaskStatus.CANCELLED)
    ]
    assert runs.list_runs(task.task_id) == [failed]


def test_provider_ref_survives_dispatch_persistence_and_collect(
    db_path: str, tmp_path: Path
) -> None:
    """Cloud routing state survives the reconstruction and SQLite boundary."""
    from factory.orchestration import RunTrackingService

    class RoutedAdapter(FakeAgentAdapter):
        def dispatch(self, task: FactoryTask, workspace: Workspace) -> AgentRun:
            produced = super().dispatch(task, workspace)
            produced.provider_ref = "sandbox:conversation:" + "a" * 40
            return produced

        def collect(self, run: AgentRun) -> AgentRun:
            assert run.provider_ref == "sandbox:conversation:" + "a" * 40
            return super().collect(run)

    tasks = _tasks(db_path)
    task = _ready_task(tasks)
    adapter = RoutedAdapter()
    run = _service(db_path, tmp_path).dispatch(task.task_id, adapter)
    assert run.provider_ref == "sandbox:conversation:" + "a" * 40
    stored = _runs(db_path).get_run(run.run_id)
    assert stored is not None and stored.provider_ref == run.provider_ref
    RunTrackingService(tasks, _runs(db_path)).refresh(run.run_id, adapter)
    assert adapter.collected == 1
