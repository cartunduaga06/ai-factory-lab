"""Completion retries, project isolation and deterministic metrics."""

from __future__ import annotations

from pathlib import Path

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
    def __init__(self, complete: bool) -> None:
        self.complete = complete

    def evidence(self, identity: FeedbackIdentity) -> DeliveryEvidence:
        return DeliveryEvidence(True, True, self.complete, True)


class Issues(IssueCompletionSink):
    def __init__(self) -> None:
        self.closed: set[tuple[str, int]] = set()

    def complete(self, identity: FeedbackIdentity) -> None:
        self.closed.add((identity.repository_slug, identity.issue_number))


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


def _records(path: Path, project: str, number: int, card: str) -> tuple[FactoryTask, AgentRun]:
    database = str(path)
    tasks = SqliteTaskRepository(database)
    runs = SqliteRunRepository(database)
    prs = SqlitePullRequestRepository(database)
    links = SqliteBacklogLinkRepository(database)
    tasks.initialize()
    item = WorkItem(
        "trello", card, "Work", f"project_id: {project}", f"example/{project}", True, True, project
    )
    links.reserve(item)
    links.begin_write(item)
    links.complete(
        item,
        MaterializedIssue(
            f"example/{project}", number, f"https://github.com/example/{project}/issues/{number}"
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
        repository_slug=task.target_repository, branch=f"factory/{card}", path=f"/tmp/{card}"
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

    def service(complete: bool) -> FeedbackReconciliationService:
        database = str(path)
        return FeedbackReconciliationService(
            SqliteTaskRepository(database),
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
    assert service(True).reconcile(first, first_run)
    assert issues.closed == {("example/project-a", 5)}
    assert cards.phases["carda"] == "DONE"
    assert service(True).reconcile(second, second_run)
    assert issues.closed == {("example/project-a", 5), ("example/project-b", 5)}
    assert service(True).reconcile(first, first_run)
    names = [event.name for event in SqliteAuditEventStore(str(path)).for_task(first.task_id)]
    assert names.count("DeliveryReconciled") == 1
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
