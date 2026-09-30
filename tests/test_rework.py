"""A second human review cycle stays on the original task, branch and PR."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, RepositoryRole, RunStatus, TaskStatus
from factory.domain.errors import PullRequestIdentityError
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PullRequest,
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
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.retry import RetryService
from factory.orchestration.rework import ReworkNotAllowedError, ReworkService, sanitize_feedback
from factory.orchestration.runtime import FactoryRuntime
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeQualityGateRunner,
    FakeRevisionInspector,
    FakeWorkspaceProvisioner,
    specs,
)
from tests.test_runtime import FakeIssueSource


class OpenState(PullRequestStateSource):
    def __init__(self) -> None:
        self.value = PullRequestState.OPEN

    def state(self, pull_request: PullRequest) -> PullRequestState:
        assert pull_request.number is not None
        return self.value


class CapturingCodex(FakeAgentAdapter):
    def __init__(self) -> None:
        super().__init__(kind=AgentKind.CODEX, status=RunStatus.SUCCEEDED)
        self.instructions: list[str] = []

    def dispatch(self, task: FactoryTask, workspace: Workspace) -> AgentRun:
        self.instructions.append(task.body)
        return super().dispatch(task, workspace)


def test_second_review_cycle_reuses_branch_and_pr(tmp_path: Path) -> None:
    db = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(db)
    runs = SqliteRunRepository(db)
    prs = SqlitePullRequestRepository(db)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = FactoryTask(
        title="QA loop",
        body="Original implementation request",
        target_repository="example/target",
        source=TaskSource("github", "example/control", 50),
    )
    adapter = CapturingCodex()
    sink = FakePullRequestSink()
    publisher = FakeWorkspacePublisher()
    state = OpenState()
    runtime = FactoryRuntime(
        intake=IssueIntakeService(FakeIssueSource(task), tasks),
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=adapter,
        provisioner=FakeWorkspaceProvisioner(),
        workspace_root=str(tmp_path / "workspaces"),
        gate_specs=specs("tests"),
        gate_runner=FakeQualityGateRunner(),
        revision_inspector=FakeRevisionInspector(),
        publisher=publisher,
        pull_request_sink=sink,
        pull_requests=prs,
        pull_request_state=state,
        base_branch="main",
        poll_interval=0,
        timeout=1,
    )
    first = runtime.run_once()
    assert first.task_status is TaskStatus.WAITING_HUMAN
    assert first.pull_request_number is not None
    service = ReworkService(tasks, runs, prs, state)
    state.value = PullRequestState.CLOSED
    with pytest.raises(ReworkNotAllowedError, match="no longer open"):
        service.request(first.task_id or "", "Please revise")
    state.value = PullRequestState.OPEN
    requested = service.request(first.task_id or "", "Review 3-5 ECC skills; run ruff format.")
    assert requested.status is TaskStatus.CHANGES_REQUESTED
    assert (
        tasks.latest_rework_feedback(requested.task_id) == "Review 3-5 ECC skills; run ruff format."
    )
    with pytest.raises(ReworkNotAllowedError):
        service.request(requested.task_id, "duplicate")
    original_pr = prs.find_by_branch("example/target", first.branch or "")
    assert original_pr is not None and original_pr.number is not None
    sink.seed(replace(original_pr, number=original_pr.number + 1))
    with pytest.raises(PullRequestIdentityError):
        runtime.run_once()
    assert tasks.get(requested.task_id).status is TaskStatus.VALIDATING  # type: ignore[union-attr]
    assert publisher.calls == 1
    sink.seed(original_pr)
    second = runtime.run_once()
    assert second.task_status is TaskStatus.WAITING_HUMAN
    assert second.task_id == first.task_id
    assert second.branch == first.branch
    assert second.pull_request_number == first.pull_request_number
    assert second.run_id != first.run_id
    assert len(runs.list_runs(requested.task_id)) == 2
    assert sink.create_calls == 1
    assert publisher.calls == 2
    assert adapter.dispatched[0][1] == adapter.dispatched[1][1]
    assert "Review 3-5 ECC skills" in adapter.instructions[1]
    assert "Review 3-5 ECC skills" not in adapter.instructions[0]
    assert [item.to_status for item in tasks.history(requested.task_id)].count(
        TaskStatus.WAITING_HUMAN
    ) == 2
    service.request(requested.task_id, "One more QA correction")
    adapter._status = RunStatus.FAILED
    failed = runtime.run_once()
    assert failed.task_status is TaskStatus.BLOCKED
    assert RetryService(tasks, runs).retry(requested.task_id).status is TaskStatus.READY
    adapter._status = RunStatus.SUCCEEDED
    third = runtime.run_once()
    assert third.task_status is TaskStatus.WAITING_HUMAN
    assert third.pull_request_number == first.pull_request_number
    assert sink.create_calls == 1


def test_request_refuses_stale_or_closed_review(tmp_path: Path) -> None:
    db = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(db)
    runs = SqliteRunRepository(db)
    prs = SqlitePullRequestRepository(db)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = tasks.save(FactoryTask(title="Unreviewed", target_repository="example/target"))
    state = OpenState()
    service = ReworkService(tasks, runs, prs, state)
    with pytest.raises(ReworkNotAllowedError):
        service.request(task.task_id, "changes")
    for status in (
        TaskStatus.READY,
        TaskStatus.CLAIMED,
        TaskStatus.RUNNING,
        TaskStatus.VALIDATING,
        TaskStatus.PR_OPEN,
        TaskStatus.WAITING_HUMAN,
    ):
        current = tasks.get(task.task_id)
        assert current is not None
        tasks.apply_transition(task.task_id, current.status, status)
    with pytest.raises(ReworkNotAllowedError, match="missing or unsuccessful"):
        service.request(task.task_id, "changes")
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN  # type: ignore[union-attr]


def test_feedback_rejects_secrets_and_controls() -> None:
    for raw in ("", "hello\x1b[31m", "token=abc", "ghp_123456", "x" * 4001):
        with pytest.raises(ReworkNotAllowedError):
            sanitize_feedback(raw)
    assert sanitize_feedback("  fix formatting\n  ") == "fix formatting"
