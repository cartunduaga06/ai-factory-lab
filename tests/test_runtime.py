"""Offline end-to-end tests for the one-shot runtime."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.domain.enums import (
    AgentKind,
    QualityGateStatus,
    RepositoryRole,
    RunStatus,
    TaskStatus,
)
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PullRequest,
    QualityGate,
    Repository,
    TaskSource,
    Workspace,
)
from factory.domain.ports import PullRequestState, PullRequestStateSource
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.integrations.context.repository import RepositoryContextSource
from factory.integrations.github.client import GitHubRequestError
from factory.integrations.openhands import WorkspacePathError, WorkspacePathMapper
from factory.orchestration.context import ContextPackBuilder
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.recovery import RecoveryPolicy
from factory.orchestration.retry import RetryService
from factory.orchestration.runtime import FactoryRuntime
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeQualityGateRunner,
    FakeRevisionInspector,
    FakeSecurityReviewGate,
    FakeWorkspaceProvisioner,
    specs,
)


class FakeIssueSource:
    def __init__(self, task: FactoryTask) -> None:
        self.task = task
        self.state = "open"
        self.labels = {"factory-ready"}
        self.eligibility_checks = 0
        self.eligibility_error: Exception | None = None

    def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
        del repository
        return [self.task] if self.state == "open" and "factory-ready" in self.labels else []

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        del repository, source
        return self.task

    def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
        del repository
        self.eligibility_checks += 1
        if self.eligibility_error is not None:
            raise self.eligibility_error
        return (
            source == self.task.source and self.state == "open" and "factory-ready" in self.labels
        )


class RepositoryAwareIssueSource:
    def __init__(self, new_task: FactoryTask, eligibility: dict[str, bool]) -> None:
        self.new_task = new_task
        self.eligibility = eligibility
        self.checks: list[tuple[Repository, TaskSource]] = []

    def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
        return [self.new_task] if self.eligibility[repository.slug] else []

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        raise NotImplementedError

    def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
        self.checks.append((repository, source))
        return self.eligibility[repository.slug]


@pytest.fixture
def runtime_parts(tmp_path: Path):
    db = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(db)
    runs = SqliteRunRepository(db)
    prs = SqlitePullRequestRepository(db)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = FactoryTask(
        title="Issue 8 acceptance",
        body="exercise the factory",
        target_repository="example/target",
        source=TaskSource("github", "example/control", 8),
    )
    intake = IssueIntakeService(FakeIssueSource(task), tasks)
    adapter = FakeAgentAdapter(kind=AgentKind.OTHER, status=RunStatus.SUCCEEDED)
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()
    gate_runner = FakeQualityGateRunner()
    runtime = FactoryRuntime(
        security_review=FakeSecurityReviewGate(),
        intake=intake,
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=adapter,
        provisioner=FakeWorkspaceProvisioner(),
        workspace_root=str(tmp_path / "host-workspaces"),
        gate_specs=specs("tests"),
        gate_runner=gate_runner,
        revision_inspector=FakeRevisionInspector(),
        publisher=publisher,
        pull_request_sink=sink,
        pull_requests=prs,
        base_branch="main",
        poll_interval=0,
        timeout=1,
        recovery_policy=RecoveryPolicy(base_backoff_seconds=0),
    )
    return runtime, tasks, runs, adapter, publisher, sink, gate_runner


def test_run_happy_path_reaches_waiting_human(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts

    result = runtime.run_once()

    assert result.outcome == "WAITING_HUMAN"
    assert result.task_status is TaskStatus.WAITING_HUMAN
    assert tasks.list(TaskStatus.WAITING_HUMAN)
    assert len(runs.list_runs(result.task_id)) == 1  # type: ignore[arg-type]
    assert len(adapter.dispatched) == 1
    assert publisher.calls == 1
    assert sink.create_calls == 1


@pytest.mark.parametrize("failure", ["missing", "invalid", "over_budget"])
def test_required_context_blocks_durably_without_dispatch(tmp_path: Path, failure: str) -> None:
    db = str(tmp_path / "factory.db")
    tasks, runs, prs = (
        SqliteTaskRepository(db),
        SqliteRunRepository(db),
        SqlitePullRequestRepository(db),
    )
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = FactoryTask(
        title="Issue context failure",
        target_repository="example/target",
        source=TaskSource("github", "example/control", 69),
    )
    adapter = FakeAgentAdapter()
    provisioner = FakeWorkspaceProvisioner()
    checkout = tmp_path / "source"
    if failure != "missing":
        checkout.mkdir()
        (checkout / "AGENTS.md").write_bytes(
            b"\xff" if failure == "invalid" else b"Repository rules"
        )
        (checkout / "README.md").write_text("Repository overview")
    budget = 10 if failure == "over_budget" else 48_000
    runtime = FactoryRuntime(
        security_review=FakeSecurityReviewGate(),
        intake=IssueIntakeService(FakeIssueSource(task), tasks),
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=adapter,
        provisioner=provisioner,
        workspace_root=str(tmp_path / "workspaces"),
        gate_specs=specs("tests"),
        gate_runner=FakeQualityGateRunner(),
        revision_inspector=FakeRevisionInspector(),
        publisher=FakeWorkspacePublisher(),
        pull_request_sink=FakePullRequestSink(),
        pull_requests=prs,
        base_branch="main",
        poll_interval=0,
        timeout=1,
        context_builder=ContextPackBuilder(
            (RepositoryContextSource(str(checkout)),), budget=budget
        ),
    )
    first = runtime.run_once()
    assert first.outcome == "REQUIRED_CONTEXT_BLOCKED"
    assert first.task_status is TaskStatus.BLOCKED
    assert (
        tasks.get(first.task_id).blocked_reason
        == "required context unavailable, invalid or over budget"
    )  # type: ignore[arg-type,union-attr]
    assert not runs.list_runs(first.task_id)  # type: ignore[arg-type]
    assert not adapter.dispatched and not provisioner.prepared
    assert any(
        event.name == "TaskBLOCKED"
        for event in SqliteAuditEventStore(db).for_task(first.task_id)  # type: ignore[arg-type]
    )
    assert runtime.run_once().outcome == "NO_ELIGIBLE_TASK"


def test_ready_task_with_durable_active_run_never_dispatches_again(runtime_parts) -> None:
    runtime, tasks, runs, adapter, _, _, _ = runtime_parts
    task = tasks.save(
        FactoryTask(
            "Interrupted",
            "example/target",
            source=TaskSource("github", "example/control", 8),
            status=TaskStatus.READY,
        )
    )
    run = runs.save_run(
        AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=RunStatus.PENDING)
    )

    result = runtime.run_once()

    assert result.outcome == "ACTIVE_RUN_STATE_MISMATCH"
    assert result.run_id == run.run_id
    assert len(runs.list_runs(task.task_id)) == 1
    assert adapter.dispatched == []


@pytest.mark.parametrize(
    ("provider_state", "expected"),
    [
        (PullRequestState.MERGED, TaskStatus.DONE),
        (PullRequestState.CLOSED, TaskStatus.CANCELLED),
        (PullRequestState.OPEN, TaskStatus.WAITING_HUMAN),
    ],
)
def test_human_review_reconciles_from_persisted_pr(
    runtime_parts, provider_state: PullRequestState, expected: TaskStatus
) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts
    first = runtime.run_once()
    assert first.task_id is not None

    class StateSource(PullRequestStateSource):
        def state(self, pull_request: PullRequest) -> PullRequestState:
            assert pull_request.number == first.pull_request_number
            assert pull_request.run_id == first.run_id
            return provider_state

    runtime._pull_request_state = StateSource()
    second = runtime.run_once()
    assert tasks.get(first.task_id).status is expected  # type: ignore[union-attr]
    if expected is TaskStatus.CANCELLED:
        assert tasks.get(first.task_id).blocked_reason == (  # type: ignore[union-attr]
            "pull request closed without merge; human review required"
        )
    assert second.outcome == (
        "WAITING_HUMAN" if expected is TaskStatus.WAITING_HUMAN else "NO_ELIGIBLE_TASK"
    )
    assert publisher.calls == 1
    assert sink.create_calls == 1
    runtime.run_once()
    assert tasks.get(first.task_id).status is expected  # type: ignore[union-attr]


def test_pool_reconciles_waiting_pr_outside_current_sprint_without_dispatch(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts
    first = runtime.run_once()
    assert first.task_id is not None

    class MergedSource(PullRequestStateSource):
        def state(self, pull_request: PullRequest) -> PullRequestState:
            assert pull_request.run_id == first.run_id
            return PullRequestState.MERGED

    class SprintWithoutTask:
        def allows_review(self, task: FactoryTask) -> bool:
            del task
            return False

        def resume_completed(self) -> None:
            pass

        def prepare(self) -> bool:
            return True

    runtime._pull_request_state = MergedSource()
    runtime._sprint = SprintWithoutTask()  # type: ignore[assignment]

    runtime.prepare_pool()

    assert tasks.get(first.task_id).status is TaskStatus.DONE  # type: ignore[union-attr]
    assert len(runs.list_runs(first.task_id)) == 1
    assert len(adapter.dispatched) == 1
    assert publisher.calls == 1
    assert sink.create_calls == 1


def test_human_review_read_failure_preserves_waiting_state(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts
    first = runtime.run_once()

    class FailingSource(PullRequestStateSource):
        def state(self, pull_request: PullRequest) -> PullRequestState:
            raise ValueError("uncertain provider state")

    runtime._pull_request_state = FailingSource()
    runtime.run_once()
    assert tasks.get(first.task_id).status is TaskStatus.WAITING_HUMAN  # type: ignore[union-attr]


@pytest.mark.parametrize("change", ["closed", "label_removed"])
@pytest.mark.parametrize("initial_status", [TaskStatus.DISCOVERED, TaskStatus.READY])
def test_stale_unstarted_task_is_cancelled_without_dispatch(
    runtime_parts, tmp_path: Path, change: str, initial_status: TaskStatus
) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts
    runtime._intake.intake(runtime._intake_repository)
    task = tasks.list(TaskStatus.DISCOVERED)[0]
    if initial_status is TaskStatus.READY:
        runtime._dispatch.lifecycle.transition(task.task_id, TaskStatus.READY)
    source = runtime._intake._source
    assert isinstance(source, FakeIssueSource)
    if change == "closed":
        source.state = "closed"
    else:
        source.labels.remove("factory-ready")

    first = runtime.run_once()
    restarted_tasks = SqliteTaskRepository(tasks.path)
    restarted_runs = SqliteRunRepository(tasks.path)
    restarted = FactoryRuntime(
        security_review=FakeSecurityReviewGate(),
        intake=IssueIntakeService(source, restarted_tasks),
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=restarted_tasks,
        runs=restarted_runs,
        adapter=adapter,
        provisioner=FakeWorkspaceProvisioner(),
        workspace_root=str(tmp_path / "restart-workspaces"),
        gate_specs=specs("tests"),
        gate_runner=FakeQualityGateRunner(),
        revision_inspector=FakeRevisionInspector(),
        publisher=publisher,
        pull_request_sink=sink,
        pull_requests=SqlitePullRequestRepository(tasks.path),
        base_branch="main",
    )
    second = restarted.run_once()
    stored = SqliteTaskRepository(tasks.path).get(task.task_id)

    assert first.outcome == "SOURCE_INELIGIBLE"
    assert first.task_status is TaskStatus.CANCELLED
    assert second.outcome == "NO_ELIGIBLE_TASK"
    assert stored is not None and stored.status is TaskStatus.CANCELLED
    assert [(t.from_status, t.to_status) for t in tasks.history(task.task_id)] == (
        [(TaskStatus.DISCOVERED, TaskStatus.READY)] if initial_status is TaskStatus.READY else []
    ) + [(initial_status, TaskStatus.CANCELLED)]
    assert len(tasks.list()) == 1
    assert runs.list_runs(task.task_id) == []
    assert adapter.dispatched == []
    assert publisher.calls == sink.create_calls == 0
    assert not Path(runtime._dispatch._workspace_root).exists()
    assert not (tmp_path / "restart-workspaces").exists()


def test_eligible_persisted_discovered_task_dispatches_normally(runtime_parts) -> None:
    runtime, tasks, runs, adapter, _, _, _ = runtime_parts
    runtime._intake.intake(runtime._intake_repository)
    task = tasks.list(TaskStatus.DISCOVERED)[0]

    result = runtime.run_once()

    assert result.outcome == "WAITING_HUMAN"
    assert len(adapter.dispatched) == 1
    assert len(runs.list_runs(task.task_id)) == 1
    assert [(t.from_status, t.to_status) for t in tasks.history(task.task_id)][:2] == [
        (TaskStatus.DISCOVERED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
    ]


@pytest.mark.parametrize(("old_eligible", "new_eligible"), [(True, False), (False, True)])
def test_persisted_task_rechecks_its_source_repository_after_intake_migration(
    runtime_parts, old_eligible: bool, new_eligible: bool
) -> None:
    runtime, tasks, runs, adapter, _, _, _ = runtime_parts
    old_source = TaskSource("github", "old-owner/old-repo", 8)
    old_task = tasks.save(
        FactoryTask(
            title="Persisted issue",
            body="from the former control repository",
            target_repository="example/target",
            source=old_source,
        )
    )
    new_task = FactoryTask(
        title="Unrelated issue with the same number",
        body="from the current control repository",
        target_repository="example/target",
        source=TaskSource("github", "new-owner/new-repo", 8),
    )
    source = RepositoryAwareIssueSource(
        new_task,
        {old_source.repository_slug: old_eligible, new_task.source.repository_slug: new_eligible},
    )
    runtime._intake = IssueIntakeService(source, tasks)
    runtime._intake_repository = Repository("new-owner/new-repo", role=RepositoryRole.CONTROL_PLANE)

    result = runtime.run_once()

    assert source.checks == [
        (Repository(old_source.repository_slug, role=RepositoryRole.CONTROL_PLANE), old_source)
    ]
    assert result.task_id == old_task.task_id
    assert result.outcome == ("WAITING_HUMAN" if old_eligible else "SOURCE_INELIGIBLE")
    assert tasks.get(old_task.task_id).status is (
        TaskStatus.WAITING_HUMAN if old_eligible else TaskStatus.CANCELLED
    )
    assert len(runs.list_runs(old_task.task_id)) == (1 if old_eligible else 0)
    assert len(adapter.dispatched) == (1 if old_eligible else 0)
    assert (tasks.find_by_source(new_task.source) is not None) is new_eligible


@pytest.mark.parametrize("initial_status", [TaskStatus.DISCOVERED, TaskStatus.READY])
@pytest.mark.parametrize("http_status", [404, 500])
def test_source_read_failure_leaves_unstarted_task_untouched(
    runtime_parts, initial_status: TaskStatus, http_status: int
) -> None:
    runtime, tasks, runs, adapter, _, _, _ = runtime_parts
    runtime._intake.intake(runtime._intake_repository)
    task = tasks.list(TaskStatus.DISCOVERED)[0]
    if initial_status is TaskStatus.READY:
        runtime._dispatch.lifecycle.transition(task.task_id, TaskStatus.READY)
    before = tasks.get(task.task_id)
    history = tasks.history(task.task_id)
    source = runtime._intake._source
    assert isinstance(source, FakeIssueSource)
    source.eligibility_error = GitHubRequestError(http_status, "source read failed")

    with pytest.raises(GitHubRequestError) as error:
        runtime.run_once()

    assert error.value.status == http_status
    assert tasks.get(task.task_id) == before
    assert tasks.history(task.task_id) == history
    assert runs.list_runs(task.task_id) == []
    assert adapter.dispatched == []
    assert not Path(runtime._dispatch._workspace_root).exists()


def test_retry_is_waiting_human_noop_and_does_not_duplicate_run_or_pr(runtime_parts) -> None:
    runtime, _, _, adapter, publisher, sink, _ = runtime_parts

    first = runtime.run_once()
    source = runtime._intake._source
    assert isinstance(source, FakeIssueSource)
    source.state = "closed"
    second = runtime.run_once()

    assert first.run_id == second.run_id
    assert len(adapter.dispatched) == 1
    assert publisher.calls == 1
    assert sink.create_calls == 1
    assert second.outcome == "WAITING_HUMAN"
    assert source.eligibility_checks == 1


def test_timeout_leaves_active_run_resumable(runtime_parts) -> None:
    runtime, tasks, runs, adapter, _, _, _ = runtime_parts
    adapter._status = RunStatus.PENDING  # type: ignore[attr-defined]
    adapter._collect_status = RunStatus.RUNNING  # type: ignore[attr-defined]

    result = runtime.run_once()

    assert result.outcome == "TIMEOUT_RESUMABLE"
    assert tasks.list(TaskStatus.RUNNING)
    assert runs.find_active_run(result.task_id) is not None  # type: ignore[arg-type]


def test_active_run_resume_collects_same_run_without_dispatch(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts
    adapter._status = RunStatus.PENDING  # type: ignore[attr-defined]
    adapter._collect_status = RunStatus.RUNNING  # type: ignore[attr-defined]
    runtime._timeout = 0  # type: ignore[attr-defined]

    first = runtime.run_once()
    source = runtime._intake._source
    assert isinstance(source, FakeIssueSource)
    source.labels.remove("factory-ready")
    adapter._collect_status = RunStatus.SUCCEEDED  # type: ignore[attr-defined]
    second = runtime.run_once()

    assert first.outcome == "TIMEOUT_RESUMABLE"
    assert second.outcome == "WAITING_HUMAN"
    assert second.run_id == first.run_id
    assert len(adapter.dispatched) == 1
    assert len(runs.list_runs(first.task_id)) == 1  # type: ignore[arg-type]
    assert tasks.get(first.task_id).status is TaskStatus.WAITING_HUMAN  # type: ignore[arg-type]
    assert publisher.calls == 1
    assert sink.create_calls == 1
    assert source.eligibility_checks == 1


@pytest.mark.parametrize("claimed", [False, True])
def test_ready_recovery_is_not_cancelled_when_source_changes(runtime_parts, claimed: bool) -> None:
    runtime, tasks, runs, adapter, _, _, _ = runtime_parts
    runtime._intake.intake(runtime._intake_repository)
    task = tasks.list(TaskStatus.DISCOVERED)[0]
    transitions = [TaskStatus.READY]
    if claimed:
        transitions.append(TaskStatus.CLAIMED)
    transitions.extend((TaskStatus.BLOCKED, TaskStatus.READY))
    for target in transitions:
        runtime._dispatch.lifecycle.transition(task.task_id, target)
    source = runtime._intake._source
    assert isinstance(source, FakeIssueSource)
    source.state = "closed"

    result = runtime.run_once()

    assert result.outcome == "WAITING_HUMAN"
    assert source.eligibility_checks == 0
    assert len(adapter.dispatched) == 1
    assert len(runs.list_runs(task.task_id)) == 1


def test_engine_change_cannot_resume_run_with_another_adapter(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts
    adapter._status = RunStatus.PENDING  # type: ignore[attr-defined]
    adapter._collect_status = RunStatus.RUNNING  # type: ignore[attr-defined]
    runtime._timeout = 0  # type: ignore[attr-defined]
    first = runtime.run_once()
    collected = adapter.collected

    adapter._kind = AgentKind.CODEX  # type: ignore[attr-defined]
    second = runtime.run_once()

    assert second.outcome == "ENGINE_MISMATCH"
    assert second.run_id == first.run_id
    assert adapter.collected == collected
    assert len(adapter.dispatched) == 1
    assert runs.find_active_run(first.task_id) is not None  # type: ignore[arg-type]
    assert tasks.get(first.task_id).status is TaskStatus.RUNNING  # type: ignore[arg-type]
    assert publisher.calls == sink.create_calls == 0


def test_validated_run_resume_skips_agent_and_continues_to_publication(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, _ = runtime_parts
    runtime._intake.intake(runtime._intake_repository)  # type: ignore[attr-defined]
    task = tasks.list(TaskStatus.DISCOVERED)[0]
    for target in (TaskStatus.READY, TaskStatus.CLAIMED, TaskStatus.RUNNING, TaskStatus.VALIDATING):
        runtime._dispatch.lifecycle.transition(task.task_id, target)  # type: ignore[attr-defined]
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch=f"factory/{task.task_id}/validated",
        path=str(Path(runtime._dispatch._workspace_root) / "validated"),  # type: ignore[attr-defined]
    )
    Path(workspace.path).mkdir(parents=True, exist_ok=True)
    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.OTHER,
        status=RunStatus.SUCCEEDED,
        workspace=workspace,
        gates=(QualityGate("tests", QualityGateStatus.PASSED, required=True),),
        validated_revision="tree-validated",
    )
    runs.save_run(run)

    result = runtime.run_once()

    assert result.outcome == "WAITING_HUMAN"
    assert result.run_id == run.run_id
    assert adapter.dispatched == []
    assert publisher.calls == 1
    assert sink.create_calls == 1


def test_failed_required_gate_does_not_publish(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, gate_runner = runtime_parts
    gate_runner._statuses["tests"] = QualityGateStatus.FAILED  # type: ignore[attr-defined]

    result = runtime.run_once()

    assert result.outcome == "QUALITY_GATES_FAILED"
    assert result.task_status is TaskStatus.READY
    assert publisher.calls == 0
    assert sink.create_calls == 0
    failed = runs.get_run(result.run_id)
    assert failed is not None and failed.workspace is not None
    assert failed.validated_revision is None

    gate_runner._statuses["tests"] = QualityGateStatus.PASSED  # type: ignore[attr-defined]
    corrected = runtime.run_once()
    assert corrected.outcome == "WAITING_HUMAN"
    assert corrected.task_status is TaskStatus.WAITING_HUMAN
    assert corrected.branch == failed.workspace.branch
    assert len(runs.list_runs(result.task_id)) == 2
    assert runs.list_runs(result.task_id)[-1].workspace == failed.workspace
    assert len(adapter.dispatched) == 2
    assert publisher.calls == sink.create_calls == 1


def test_quality_correction_limit_stops_new_runs(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, gate_runner = runtime_parts
    runtime._recovery_policy = RecoveryPolicy(correction_limit=1, base_backoff_seconds=0)
    gate_runner._statuses["tests"] = QualityGateStatus.FAILED  # type: ignore[attr-defined]

    first = runtime.run_once()
    second = runtime.run_once()
    third = runtime.run_once()

    assert first.task_status is TaskStatus.READY
    assert second.task_status is TaskStatus.BLOCKED
    assert third.outcome == "NO_ELIGIBLE_TASK"
    assert len(runs.list_runs(first.task_id)) == 2
    assert len(adapter.dispatched) == 2
    assert tasks.get(first.task_id).blocked_reason == "quality correction limit reached"
    assert publisher.calls == sink.create_calls == 0


def test_quality_correction_backoff_uses_persisted_finish_time(runtime_parts) -> None:
    from datetime import UTC, datetime, timedelta

    runtime, _, runs, adapter, _, _, gate_runner = runtime_parts
    runtime._recovery_policy = RecoveryPolicy(base_backoff_seconds=60)
    gate_runner._statuses["tests"] = QualityGateStatus.FAILED  # type: ignore[attr-defined]
    first = runtime.run_once()

    assert runtime.run_once().outcome == "BACKOFF_PENDING"
    assert len(runs.list_runs(first.task_id)) == 1
    old = runs.get_run(first.run_id)
    assert old is not None
    old.finished_at = datetime.now(UTC) - timedelta(seconds=61)
    runs.update_run(old)
    gate_runner._statuses["tests"] = QualityGateStatus.PASSED  # type: ignore[attr-defined]

    assert runtime.run_once().outcome == "WAITING_HUMAN"
    assert len(adapter.dispatched) == 2


def test_failed_qa_correction_remains_recoverable_on_same_branch(runtime_parts) -> None:
    runtime, tasks, runs, adapter, publisher, sink, gate_runner = runtime_parts
    gate_runner._statuses["tests"] = QualityGateStatus.FAILED  # type: ignore[attr-defined]
    first = runtime.run_once()
    adapter._status = RunStatus.FAILED  # type: ignore[attr-defined]
    correction = runtime.run_once()
    assert correction.task_status is TaskStatus.BLOCKED
    assert publisher.calls == sink.create_calls == 0

    RetryService(tasks, runs).retry(first.task_id)
    adapter._status = RunStatus.SUCCEEDED  # type: ignore[attr-defined]
    gate_runner._statuses["tests"] = QualityGateStatus.PASSED  # type: ignore[attr-defined]
    recovered = runtime.run_once()
    assert recovered.task_status is TaskStatus.WAITING_HUMAN
    assert recovered.branch == first.branch
    assert len({run.workspace.workspace_id for run in runs.list_runs(first.task_id)}) == 1


def test_host_path_mapping_is_explicit_and_rejects_escape(tmp_path: Path) -> None:
    mapper = WorkspacePathMapper(str(tmp_path / "host"), "/projects")
    inside = tmp_path / "host" / "ws-1"

    assert mapper.to_container(str(inside)) == "/projects/ws-1"
    with pytest.raises(WorkspacePathError):
        mapper.to_container(str(tmp_path / "elsewhere" / "ws-1"))


@pytest.mark.parametrize("failed", [False, True])
def test_legacy_validating_runtime_revalidates_without_agent_or_new_records(
    runtime_parts, failed: bool
) -> None:
    runtime, tasks, runs, adapter, publisher, sink, runner = runtime_parts
    runtime._intake.intake(runtime._intake_repository)
    task = tasks.list(TaskStatus.DISCOVERED)[0]
    for target in (TaskStatus.READY, TaskStatus.CLAIMED, TaskStatus.RUNNING, TaskStatus.VALIDATING):
        runtime._dispatch.lifecycle.transition(task.task_id, target)
    root = Path(runtime._dispatch._workspace_root)
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch=f"factory/{task.task_id}/legacy",
        path=str(root / "legacy"),
    )
    Path(workspace.path).mkdir(parents=True)
    run = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.SUCCEEDED,
            workspace=workspace,
            gates=(QualityGate("tests", QualityGateStatus.PASSED),),
        )
    )
    if failed:
        runner._statuses["tests"] = QualityGateStatus.FAILED

    result = runtime.run_once()
    stored = runs.get_run(run.run_id)
    assert stored is not None
    assert result.run_id == run.run_id
    assert runner.calls == [("tests", workspace.path)]
    assert adapter.dispatched == [] and adapter.collected == 0
    assert len(runs.list_runs(task.task_id)) == 1
    assert stored.workspace == workspace
    assert runs.get_workspace(workspace.workspace_id) == workspace
    assert list(root.iterdir()) == [Path(workspace.path)]
    assert (stored.validated_revision is not None) == (not failed)
    assert publisher.calls == sink.create_calls == (0 if failed else 1)
    assert result.outcome == ("QUALITY_GATES_FAILED" if failed else "WAITING_HUMAN")
    assert result.task_status is (TaskStatus.READY if failed else TaskStatus.WAITING_HUMAN)
    if not failed:
        runtime.run_once()
        assert runner.calls == [("tests", workspace.path)]
        assert adapter.dispatched == [] and adapter.collected == 0

def test_human_review_provider_failure_is_deferred(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    runtime = FactoryRuntime.__new__(FactoryRuntime)
    runtime._pull_request_state = object()
    task = SimpleNamespace(task_id="task-1")
    pending = iter([[task], [], [], [], []])
    runtime._tasks = SimpleNamespace(list=lambda _status: next(pending))
    calls: list[str] = []

    def fail(_task: object) -> None:
        calls.append("task-1")
        raise RuntimeError("provider unavailable")

    monkeypatch.setattr(runtime, "_reconcile_human_review_task", fail)
    runtime._reconcile_human_reviews()

    assert calls == ["task-1"]
