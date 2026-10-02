"""First-class post-rebase revalidation creates new evidence without rewriting runs."""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus, TaskStatus
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PullRequest,
    QualityGate,
    QualityGateSpec,
    TaskSource,
    Workspace,
)
from factory.domain.projects import ProjectProfile, ProjectRegistry
from factory.domain.revalidation import (
    ExactHeadCiEvidence,
    ExactHeadCiStatus,
    PostRebaseRevalidation,
    RevalidationResult,
    WorkspaceRevisionSnapshot,
)
from factory.domain.security import SecurityFinding, SecurityReview
from factory.infrastructure.persistence import (
    SqlitePostRebaseRevalidationRepository,
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.orchestration.revalidation import (
    REVALIDATION_BLOCK_REASON,
    PostRebaseRevalidationService,
)

OLD_HEAD = "39e9863d952d31899417d1b308f74ca5f9e1c485"
NEW_HEAD = "101527debd1d367faa86522050ef4b5596e374d7"
TREE = "4e37f2b39e720caf293795bb4cfd2cb12d946f6a"


class GateRunner:
    def run(self, spec: QualityGateSpec, workspace: Workspace) -> QualityGate:
        del workspace
        return QualityGate(spec.name, QualityGateStatus.PASSED, "exit_code=0", spec.required)


class WorkspaceInspector:
    def __init__(self) -> None:
        self.snapshot_value = WorkspaceRevisionSnapshot(NEW_HEAD, TREE)

    def snapshot(self, workspace: Workspace) -> WorkspaceRevisionSnapshot:
        del workspace
        return self.snapshot_value


class SecurityInspector:
    def __init__(self, *, critical: bool = False) -> None:
        self.critical = critical

    def inspect(self, task: FactoryTask, run: AgentRun) -> SecurityReview:
        del task
        findings = (SecurityFinding("test-critical", "digest", 1),) if self.critical else ()
        return SecurityReview("test-rules", f"{run.validated_revision}:base", findings)


class Provider:
    def __init__(self, status: ExactHeadCiStatus = ExactHeadCiStatus.PENDING) -> None:
        self.status = status
        self.expected: list[str] = []

    def current_head(self, pull_request: PullRequest) -> str:
        assert pull_request.commit_sha == OLD_HEAD
        return NEW_HEAD

    def exact_ci(
        self,
        pull_request: PullRequest,
        expected_head: str,
        required_checks: tuple[str, ...],
        *,
        required: bool,
    ) -> ExactHeadCiEvidence:
        assert pull_request.number == 116
        assert required
        assert required_checks == ("ci-3.11", "ci-3.12")
        self.expected.append(expected_head)
        return ExactHeadCiEvidence(expected_head, self.status, required_checks)


def _registry() -> ProjectRegistry:
    return ProjectRegistry(
        (
            ProjectProfile(
                "ai-factory-lab",
                "example/target",
                "/tmp/source",
                "main",
                (QualityGateSpec("tests", ("pytest",)),),
                required_ci_checks=("ci-3.11", "ci-3.12"),
            ),
        )
    )


def _records(
    path: str,
) -> tuple[
    SqliteTaskRepository,
    SqliteRunRepository,
    SqlitePullRequestRepository,
    SqlitePostRebaseRevalidationRepository,
    FactoryTask,
    AgentRun,
]:
    tasks = SqliteTaskRepository(path)
    runs = SqliteRunRepository(path)
    prs = SqlitePullRequestRepository(path)
    attempts = SqlitePostRebaseRevalidationRepository(path)
    attempts.initialize()
    task = tasks.save(
        FactoryTask(
            "Control Tower",
            "example/target",
            source=TaskSource("github", "example/target", 115),
            status=TaskStatus.BLOCKED,
            blocked_reason=REVALIDATION_BLOCK_REASON,
        )
    )
    workspace = Workspace(
        workspace_id="workspace-1",
        repository_slug="example/target",
        branch=f"factory/{task.task_id}/workspace",
        path="/tmp/revalidation-workspace",
    )
    run = runs.save_run(
        AgentRun(
            task.task_id,
            AgentKind.CODEX,
            run_id="source-run",
            status=RunStatus.SUCCEEDED,
            workspace=workspace,
            gates=(QualityGate("tests", QualityGateStatus.PASSED, "exit_code=0"),),
            validated_revision="historical-tree",
            project_id=task.project_id,
        )
    )
    prs.save(
        PullRequest(
            "example/target",
            workspace.branch,
            "main",
            "Control Tower",
            number=116,
            task_id=task.task_id,
            run_id=run.run_id,
            commit_sha=OLD_HEAD,
        )
    )
    return tasks, runs, prs, attempts, task, run


def _service(path: str, provider: Provider, *, critical: bool = False):
    tasks = SqliteTaskRepository(path)
    runs = SqliteRunRepository(path)
    prs = SqlitePullRequestRepository(path)
    attempts = SqlitePostRebaseRevalidationRepository(path)
    return PostRebaseRevalidationService(
        tasks,
        runs,
        prs,
        attempts,
        GateRunner(),
        WorkspaceInspector(),
        SecurityInspector(critical=critical),
        provider,
        registry=_registry(),
    )


def test_mismatch_revalidation_waits_for_exact_ci_then_rebinds_without_rewriting_run(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "factory.db")
    tasks, runs, prs, attempts, task, historical = _records(path)
    before = runs.get_run(historical.run_id)
    provider = Provider(ExactHeadCiStatus.PENDING)

    waiting = _service(path, provider).revalidate(task.task_id)

    assert waiting.task_status is TaskStatus.BLOCKED
    assert waiting.attempt.result is RevalidationResult.WAITING_CI
    assert waiting.attempt.previous_head == OLD_HEAD
    assert waiting.attempt.new_head == NEW_HEAD
    assert waiting.attempt.validated_tree == TREE
    assert waiting.attempt.security_review is not None
    assert waiting.attempt.ci_sha is None
    assert prs.get_for_run(historical.run_id).commit_sha == OLD_HEAD  # type: ignore[union-attr]
    assert runs.get_run(historical.run_id) == before

    provider.status = ExactHeadCiStatus.PASSED
    completed = _service(path, provider).revalidate(task.task_id)

    assert completed.task_status is TaskStatus.WAITING_HUMAN
    assert completed.attempt.result is RevalidationResult.PASSED
    assert completed.attempt.ci_sha == NEW_HEAD
    assert completed.attempt.ci_checks == ("ci-3.11", "ci-3.12")
    rebound = prs.get_for_run(historical.run_id)
    assert rebound is not None and rebound.commit_sha == NEW_HEAD
    assert rebound.run_id == historical.run_id
    assert runs.get_run(historical.run_id) == before
    assert tasks.get(task.task_id).blocked_reason is None  # type: ignore[union-attr]
    assert provider.expected == [NEW_HEAD, NEW_HEAD]

    repeated = _service(path, provider).revalidate(task.task_id)
    assert repeated.attempt.attempt_id == completed.attempt.attempt_id
    assert len([a for a in (attempts.latest_for_task(task.task_id),) if a is not None]) == 1
    names = [event.name for event in SqliteAuditEventStore(path).for_task(task.task_id)]
    assert names.count("RevalidationStarted") == 1
    assert names.count("RevalidationWaitingCI") == 1
    assert names.count("RevalidationPassed") == 1


def test_exact_ci_failure_keeps_task_blocked_and_old_pr_head(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks, _, prs, _, task, historical = _records(path)
    provider = Provider(ExactHeadCiStatus.FAILED)

    outcome = _service(path, provider).revalidate(task.task_id)

    assert outcome.attempt.result is RevalidationResult.FAILED
    assert outcome.attempt.failure_reason == "provider_ci_failed"
    assert outcome.attempt.ci_sha == NEW_HEAD
    assert outcome.task_status is TaskStatus.BLOCKED
    assert prs.get_for_run(historical.run_id).commit_sha == OLD_HEAD  # type: ignore[union-attr]
    assert tasks.get(task.task_id).blocked_reason == REVALIDATION_BLOCK_REASON  # type: ignore[union-attr]


def test_security_failure_is_durable_and_never_rebinds(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    _, _, prs, attempts, task, historical = _records(path)

    outcome = _service(path, Provider(), critical=True).revalidate(task.task_id)

    assert outcome.attempt.result is RevalidationResult.FAILED
    assert outcome.attempt.failure_reason == "security_review_failed"
    assert outcome.attempt.validated_tree == TREE
    assert outcome.attempt.security_review is not None
    assert outcome.attempt.security_review.critical
    assert prs.get_for_run(historical.run_id).commit_sha == OLD_HEAD  # type: ignore[union-attr]
    assert attempts.active_for_task(task.task_id) is None


def test_repository_prevents_two_different_active_revalidations(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    _, _, _, attempts, task, historical = _records(path)
    first = PostRebaseRevalidation(
        task.task_id,
        historical.run_id,
        "example/target",
        historical.workspace.branch,  # type: ignore[union-attr]
        116,
        OLD_HEAD,
        NEW_HEAD,
    )
    stored = attempts.start(first)
    duplicate = attempts.start(
        PostRebaseRevalidation(
            task.task_id,
            historical.run_id,
            "example/target",
            historical.workspace.branch,  # type: ignore[union-attr]
            116,
            OLD_HEAD,
            NEW_HEAD,
        )
    )
    assert duplicate.attempt_id == stored.attempt_id
    with pytest.raises(ValueError, match="another post-rebase revalidation is active"):
        attempts.start(
            PostRebaseRevalidation(
                task.task_id,
                historical.run_id,
                "example/target",
                historical.workspace.branch,  # type: ignore[union-attr]
                116,
                OLD_HEAD,
                "f" * 40,
            )
        )
