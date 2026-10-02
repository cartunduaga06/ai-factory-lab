"""Completion retries, project isolation and deterministic metrics."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from factory.domain.backlog import MaterializedIssue, WorkItem
from factory.domain.enums import AgentKind, RunStatus, TaskStatus
from factory.domain.feedback import DeliveryEvidence, FeedbackIdentity
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PullRequest,
    QualityGateSpec,
    TaskSource,
    Workspace,
)
from factory.domain.ports import DeliveryEvidenceSource, IssueCompletionSink, WorkItemFeedbackSink
from factory.domain.projects import ProjectProfile, ProjectRegistry
from factory.domain.sprint import SprintManifest, SprintStep
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.backlog_sqlite import SqliteBacklogLinkRepository
from factory.infrastructure.persistence.feedback_sqlite import SqliteFeedbackEventRepository
from factory.infrastructure.persistence.metrics import SqliteSprintMetrics
from factory.infrastructure.persistence.pr_sqlite import SqlitePullRequestRepository
from factory.infrastructure.persistence.run_sqlite import SqliteRunRepository
from factory.infrastructure.persistence.sprint_sqlite import SqliteSprintRepository
from factory.infrastructure.persistence.sqlite import SqliteTaskRepository
from factory.orchestration.feedback import FeedbackReconciliationService


class Evidence(DeliveryEvidenceSource):
    def __init__(self, complete: bool, merged: bool = True) -> None:
        self.complete = complete
        self.merged = merged

    def evidence(self, identity: FeedbackIdentity) -> DeliveryEvidence:
        return DeliveryEvidence(self.merged, True, self.complete, True)


class Issues(IssueCompletionSink):
    def __init__(self) -> None:
        self.closed: set[tuple[str, int]] = set()
        self.reasons: dict[tuple[str, int], str] = {}

    def complete(self, identity: FeedbackIdentity) -> None:
        self.closed.add((identity.repository_slug, identity.issue_number))

    def state(self, repository_slug: str, issue_number: int) -> tuple[str, str | None]:
        key = (repository_slug, issue_number)
        if key not in self.closed:
            return "open", None
        return "closed", self.reasons.get(key, "completed")

    def close(self, identity: FeedbackIdentity, reason: str) -> None:
        key = (identity.repository_slug, identity.issue_number)
        self.closed.add(key)
        self.reasons[key] = reason


class Cards(WorkItemFeedbackSink):
    def __init__(self) -> None:
        self.phases: dict[str, str] = {}

    def sync(self, identity: FeedbackIdentity, phase: str) -> None:
        assert identity.work_item_id is not None
        self.phases[identity.work_item_id] = phase


def _registry() -> ProjectRegistry:
    return ProjectRegistry(
        tuple(
            ProjectProfile(
                project_id=project,
                repository_slug=f"example/{project}",
                source_checkout=f"/tmp/{project}",
                base_ref="main",
                gates=(QualityGateSpec("tests", ("pytest",)),),
            )
            for project in ("project-a", "project-b")
        )
    )


def _records(
    path: Path, project: str, number: int, card: str | None
) -> tuple[FactoryTask, AgentRun]:
    database = str(path)
    tasks = SqliteTaskRepository(database)
    runs = SqliteRunRepository(database)
    prs = SqlitePullRequestRepository(database)
    links = SqliteBacklogLinkRepository(database)
    tasks.initialize()
    if card is not None:
        item = WorkItem(
            "trello",
            card,
            "Work",
            f"project_id: {project}",
            f"example/{project}",
            True,
            True,
            project,
        )
        links.reserve(item)
        links.begin_write(item)
        links.complete(
            item,
            MaterializedIssue(
                f"example/{project}",
                number,
                f"https://github.com/example/{project}/issues/{number}",
            ),
        )
    task = tasks.save(
        FactoryTask(
            "Work",
            f"example/{project}",
            source=TaskSource("github", f"example/{project}", number),
            status=TaskStatus.WAITING_HUMAN,
            project_id=project,
        )
    )
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch=f"factory/{card or number}",
        path=f"/tmp/{card or number}",
    )
    run = runs.save_run(
        AgentRun(
            task.task_id,
            AgentKind.CODEX,
            status=RunStatus.SUCCEEDED,
            workspace=workspace,
            project_id=project,
        )
    )
    prs.save(
        PullRequest(
            task.target_repository,
            workspace.branch,
            "main",
            "Work",
            task_id=task.task_id,
            run_id=run.run_id,
            number=number + 100,
            commit_sha="a" * 40,
        )
    )
    return task, run


def _service(
    path: Path, complete: bool, issues: Issues, cards: Cards
) -> FeedbackReconciliationService:
    database = str(path)
    return FeedbackReconciliationService(
        SqliteTaskRepository(database),
        SqliteRunRepository(database),
        SqlitePullRequestRepository(database),
        Evidence(complete),
        issues,
        cards,
        SqliteFeedbackEventRepository(database),
        SqliteBacklogLinkRepository(database),
        _registry(),
        SqliteSprintRepository(database),
    )


def test_github_direct_merge_then_complete_is_idempotent_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "direct.db"
    task, run = _records(path, "project-a", 84, None)
    issues, cards = Issues(), Cards()
    assert not _service(path, False, issues, cards).reconcile(task, run)
    assert SqlitePullRequestRepository(str(path)).get_for_run(run.run_id).merged  # type: ignore[union-attr]
    assert SqliteTaskRepository(str(path)).get(task.task_id).status is TaskStatus.WAITING_HUMAN  # type: ignore[union-attr]
    assert issues.closed == set()
    assert cards.phases == {}
    assert _service(path, True, issues, cards).reconcile(task, run)
    assert _service(path, True, issues, cards).reconcile(task, run)
    assert SqliteTaskRepository(str(path)).get(task.task_id).status is TaskStatus.DONE  # type: ignore[union-attr]
    assert issues.closed == {("example/project-a", 84)}
    assert cards.phases == {}
    names = [event.name for event in SqliteAuditEventStore(str(path)).for_task(task.task_id)]
    assert names.count("PRUpdated") == 1
    assert names.count("DeliveryReconciled") == 1


def test_superseded_run_cannot_reconcile_reworked_pr(tmp_path: Path) -> None:
    path = tmp_path / "rework.db"
    first_task, first = _records(path, "project-a", 86, None)
    runs = SqliteRunRepository(str(path))
    prs = SqlitePullRequestRepository(str(path))
    first_pr = prs.get_for_run(first.run_id)
    assert first_pr is not None
    second = runs.save_run(
        AgentRun(
            first.task_id,
            AgentKind.CODEX,
            status=RunStatus.SUCCEEDED,
            workspace=first.workspace,
            project_id=first.project_id,
        )
    )
    final_pr = prs.record_revision(first_pr, second.run_id, "b" * 40)
    issues, cards = Issues(), Cards()

    assert not _service(path, True, issues, cards).reconcile(first_task, first)
    assert not prs.get_for_run(second.run_id).merged  # type: ignore[union-attr]
    assert issues.closed == set()

    # Simulate provider-confirmed merge with complete required CI evidence.
    assert _service(path, True, issues, cards).reconcile(first_task, runs.get_run(second.run_id))  # type: ignore[arg-type]
    assert prs.get_for_run(second.run_id).merged  # type: ignore[union-attr]
    assert SqliteTaskRepository(str(path)).get(first_task.task_id).status is TaskStatus.DONE  # type: ignore[union-attr]
    assert issues.closed == {("example/project-a", 86)}
    assert not _service(path, True, issues, cards).reconcile(first_task, runs.get_run(first.run_id))  # type: ignore[arg-type]
    assert SqliteFeedbackEventRepository(str(path)).is_completed(first_task.task_id)
    assert final_pr.commit_sha == "b" * 40


def test_reworked_pr_revision_reconciles_after_service_restart(tmp_path: Path) -> None:
    path = tmp_path / "rework-restart.db"
    task, first = _records(path, "project-a", 87, None)
    runs = SqliteRunRepository(str(path))
    prs = SqlitePullRequestRepository(str(path))
    original = prs.get_for_run(first.run_id)
    assert original is not None
    second = runs.save_run(
        AgentRun(
            task.task_id,
            AgentKind.CODEX,
            status=RunStatus.SUCCEEDED,
            workspace=first.workspace,
            project_id=first.project_id,
        )
    )
    published_sha = "c" * 40
    prs.record_revision(original, second.run_id, published_sha)

    # Reconstruct every persistence-backed service after the rebind is durable.
    restarted_prs = SqlitePullRequestRepository(str(path))
    restarted_runs = SqliteRunRepository(str(path))
    durable_pr = restarted_prs.get_for_run(second.run_id)
    assert durable_pr is not None
    assert durable_pr.number == original.number
    assert durable_pr.commit_sha == published_sha
    assert restarted_prs.get_for_run(first.run_id) is None

    issues, cards = Issues(), Cards()
    service = _service(path, True, issues, cards)
    assert not service.reconcile(task, first)
    assert service.reconcile(task, restarted_runs.get_run(second.run_id))  # type: ignore[arg-type]
    assert SqliteTaskRepository(str(path)).get(task.task_id).status is TaskStatus.DONE  # type: ignore[union-attr]
    assert issues.closed == {("example/project-a", 87)}
    assert not service.reconcile(task, restarted_runs.get_run(first.run_id))  # type: ignore[arg-type]
    assert SqliteFeedbackEventRepository(str(path)).is_completed(task.task_id)


def test_trello_task_with_lost_link_cannot_downgrade(tmp_path: Path) -> None:
    path = tmp_path / "lost-link.db"
    task, run = _records(path, "project-a", 7, "carda")
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM backlog_links WHERE external_id = 'carda'")
    issues, cards = Issues(), Cards()
    assert not _service(path, True, issues, cards).reconcile(task, run)
    assert not SqlitePullRequestRepository(str(path)).get_for_run(run.run_id).merged  # type: ignore[union-attr]
    assert issues.closed == set()
    assert cards.phases == {}


def test_github_direct_link_mismatch_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "mismatch.db"
    task, run = _records(path, "project-a", 7, None)
    item = WorkItem(
        "trello",
        "carda",
        "Work",
        "project_id: project-a",
        "example/project-a",
        True,
        True,
        "project-a",
    )
    links = SqliteBacklogLinkRepository(str(path))
    links.reserve(item)
    links.begin_write(item)
    links.complete(
        item,
        MaterializedIssue("example/project-a", 7, "https://github.com/example/project-a/issues/7"),
    )
    issues, cards = Issues(), Cards()
    assert not _service(path, True, issues, cards).reconcile(task, run)
    assert not SqlitePullRequestRepository(str(path)).get_for_run(run.run_id).merged  # type: ignore[union-attr]


def test_completion_repairs_done_issue_and_is_idempotent_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    first, first_run = _records(path, "project-a", 5, "carda")
    second, second_run = _records(path, "project-b", 5, "cardb")
    SqliteSprintRepository(str(path)).authorize(
        SprintManifest(
            "sprint-one",
            (
                SprintStep(
                    WorkItem(
                        "trello",
                        "carda",
                        "Work",
                        "project_id: project-a",
                        "example/project-a",
                        True,
                        True,
                        "project-a",
                    )
                ),
                SprintStep(
                    WorkItem(
                        "trello",
                        "cardb",
                        "Work",
                        "project_id: project-b",
                        "example/project-b",
                        True,
                        True,
                        "project-b",
                    )
                ),
            ),
        )
    )
    issues, cards = Issues(), Cards()
    assert not SqlitePullRequestRepository(str(path)).get_for_run(first_run.run_id).merged  # type: ignore[union-attr]

    def service(complete: bool) -> FeedbackReconciliationService:
        database = str(path)
        return FeedbackReconciliationService(
            SqliteTaskRepository(database),
            SqliteRunRepository(database),
            SqlitePullRequestRepository(database),
            Evidence(complete),
            issues,
            cards,
            SqliteFeedbackEventRepository(database),
            SqliteBacklogLinkRepository(database),
            _registry(),
            SqliteSprintRepository(database),
        )

    assert not service(False).reconcile(first, first_run)
    assert SqliteTaskRepository(str(path)).get(first.task_id).status is TaskStatus.WAITING_HUMAN  # type: ignore[union-attr]
    assert cards.phases == {"carda": "MERGED"}
    assert SqlitePullRequestRepository(str(path)).get_for_run(first_run.run_id).merged  # type: ignore[union-attr]
    assert service(True).reconcile(first, first_run)
    assert issues.closed == {("example/project-a", 5)}
    assert cards.phases["carda"] == "DONE"
    assert service(True).reconcile(second, second_run)
    assert issues.closed == {("example/project-a", 5), ("example/project-b", 5)}
    assert service(True).reconcile(first, first_run)
    names = [event.name for event in SqliteAuditEventStore(str(path)).for_task(first.task_id)]
    assert names.count("DeliveryReconciled") == 1
    assert names.count("PRUpdated") == 1
    assert SqlitePullRequestRepository(str(path)).get_for_run(first_run.run_id).merged  # type: ignore[union-attr]
    report = SqliteSprintMetrics(str(path)).report()
    assert report[0].throughput == 2
    assert {row.project_id: row.throughput for row in report[1:]} == {
        "project-a": 1,
        "project-b": 1,
    }


def test_project_identity_cannot_reconcile_another_project_card(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    task, run = _records(path, "project-a", 7, "carda")
    issues, cards = Issues(), Cards()
    wrong = FeedbackReconciliationService(
        SqliteTaskRepository(str(path)),
        SqliteRunRepository(str(path)),
        SqlitePullRequestRepository(str(path)),
        Evidence(True),
        issues,
        cards,
        SqliteFeedbackEventRepository(str(path)),
        SqliteBacklogLinkRepository(str(path)),
        ProjectRegistry((_registry().resolve("project-b"),)),
    )
    assert not wrong.reconcile(task, run)
    assert issues.closed == set()
    assert cards.phases == {}


def test_unverified_merge_does_not_change_local_pr(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    task, run = _records(path, "project-a", 7, "carda")
    service = FeedbackReconciliationService(
        SqliteTaskRepository(str(path)),
        SqliteRunRepository(str(path)),
        SqlitePullRequestRepository(str(path)),
        Evidence(True, merged=False),
        Issues(),
        Cards(),
        SqliteFeedbackEventRepository(str(path)),
        SqliteBacklogLinkRepository(str(path)),
        _registry(),
    )
    assert not service.reconcile(task, run)
    assert not SqlitePullRequestRepository(str(path)).get_for_run(run.run_id).merged  # type: ignore[union-attr]
    assert "PRUpdated" not in [
        event.name for event in SqliteAuditEventStore(str(path)).for_task(task.task_id)
    ]


def test_provider_closed_issue_marks_done_and_records_resolution(tmp_path: Path) -> None:
    path = tmp_path / "closed-issue.db"
    task, run = _records(path, "project-a", 24, None)
    tasks = SqliteTaskRepository(str(path))
    task = tasks.get(task.task_id)
    assert task is not None
    task.status = TaskStatus.WAITING_HUMAN
    tasks.update(task)
    issues, cards = Issues(), Cards()
    issues.closed.add(("example/project-a", 24))
    service = _service(path, False, issues, cards)
    assert service.reconcile_provider_closure(task, run)
    assert tasks.get(task.task_id).status is TaskStatus.DONE  # type: ignore[union-attr]
    names = [event.name for event in SqliteAuditEventStore(str(path)).for_task(task.task_id)]
    assert names.count("DeliveryReconciled") == 1


@pytest.mark.parametrize(
    ("initial", "reason"),
    [
        (TaskStatus.FAILED, "completed"),
        (TaskStatus.CANCELLED, "duplicate"),
    ],
)
def test_terminal_provider_closure_resolves_once(
    tmp_path: Path, initial: TaskStatus, reason: str
) -> None:
    path = tmp_path / f"terminal-{initial.value}.db"
    task, run = _records(path, "project-a", 31, None)
    tasks = SqliteTaskRepository(str(path))
    stored = tasks.get(task.task_id)
    assert stored is not None
    stored.status = initial
    stored.blocked_reason = "stale provider closure blocker"
    tasks.update(stored)
    issues, cards = Issues(), Cards()
    issues.closed.add(("example/project-a", 31))
    issues.reasons = {("example/project-a", 31): reason}
    service = _service(path, False, issues, cards)

    assert service.reconcile_provider_closure(stored, run)
    assert service.reconcile_provider_closure(stored, run)
    final = tasks.get(task.task_id)
    assert final is not None and final.status is TaskStatus.DONE
    assert final.blocked_reason is None
    transitions = tasks.history(task.task_id)
    assert [(item.from_status, item.to_status) for item in transitions].count(
        (initial, TaskStatus.DONE)
    ) == 1
    events = SqliteAuditEventStore(str(path)).for_task(task.task_id)
    assert sum(event.name == "DeliveryReconciled" for event in events) == 1


def test_closed_unmerged_pr_waits_for_explicit_issue_resolution(tmp_path: Path) -> None:
    path = tmp_path / "closed-unmerged.db"
    task, run = _records(path, "project-a", 32, None)
    tasks = SqliteTaskRepository(str(path))
    issues, cards = Issues(), Cards()
    service = _service(path, False, issues, cards)
    assert not service.reconcile_provider_closure(task, run)
    stored = tasks.get(task.task_id)
    assert stored is not None and stored.status is TaskStatus.WAITING_HUMAN

    issues.closed.add(("example/project-a", 32))
    issues.reasons = {("example/project-a", 32): "not_planned"}
    assert service.reconcile_provider_closure(task, run)
    assert tasks.get(task.task_id).status is TaskStatus.DONE  # type: ignore[union-attr]


def test_reopened_issue_does_not_resurrect_done_task(tmp_path: Path) -> None:
    path = tmp_path / "reopened.db"
    task, run = _records(path, "project-a", 33, None)
    tasks = SqliteTaskRepository(str(path))
    issues, cards = Issues(), Cards()
    issues.closed.add(("example/project-a", 33))
    issues.reasons = {("example/project-a", 33): "completed"}
    service = _service(path, False, issues, cards)
    assert service.reconcile_provider_closure(task, run)
    issues.closed.clear()
    issues.reasons.clear()
    assert not service.reconcile_provider_closure(task, run)
    assert tasks.get(task.task_id).status is TaskStatus.DONE  # type: ignore[union-attr]


def test_incidental_superseded_text_does_not_resolve(tmp_path: Path) -> None:
    path = tmp_path / "incidental-superseded.db"
    task, run = _records(path, "project-a", 34, None)
    tasks = SqliteTaskRepository(str(path))
    stored = tasks.get(task.task_id)
    assert stored is not None
    stored.body = "Another task superseded this one during triage."
    tasks.update(stored)
    issues, cards = Issues(), Cards()
    service = _service(path, False, issues, cards)
    assert not service.reconcile_external(stored, run)
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN  # type: ignore[union-attr]


def test_mismatched_run_cannot_record_merge(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    task, run = _records(path, "project-a", 7, "carda")
    service = FeedbackReconciliationService(
        SqliteTaskRepository(str(path)),
        SqliteRunRepository(str(path)),
        SqlitePullRequestRepository(str(path)),
        Evidence(True),
        Issues(),
        Cards(),
        SqliteFeedbackEventRepository(str(path)),
        SqliteBacklogLinkRepository(str(path)),
        _registry(),
    )
    assert not service.reconcile(task, replace(run, run_id="other-run"))
    assert not SqlitePullRequestRepository(str(path)).get_for_run(run.run_id).merged  # type: ignore[union-attr]


def test_mismatched_workspace_cannot_record_merge(tmp_path: Path) -> None:
    path = tmp_path / "workspace.db"
    task, run = _records(path, "project-a", 7, None)
    assert run.workspace is not None
    issues, cards = Issues(), Cards()
    changed = replace(run, workspace=replace(run.workspace, workspace_id="other-workspace"))
    assert not _service(path, True, issues, cards).reconcile(task, changed)
    assert not SqlitePullRequestRepository(str(path)).get_for_run(run.run_id).merged  # type: ignore[union-attr]
    assert issues.closed == set()
