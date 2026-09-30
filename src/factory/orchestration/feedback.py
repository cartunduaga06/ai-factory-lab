"""Post-review reconciliation across a project Issue, task, PR and WorkItem."""

from __future__ import annotations

from factory.domain.enums import RunStatus, TaskKind, TaskStatus
from factory.domain.feedback import FeedbackIdentity
from factory.domain.models import AgentRun, FactoryTask, PullRequest
from factory.domain.ports import (
    BacklogLinkRepository,
    DeliveryEvidenceSource,
    FeedbackEventRepository,
    IssueCompletionSink,
    PullRequestRepository,
    SprintRepository,
    TaskRepository,
    WorkItemFeedbackSink,
)
from factory.domain.projects import ProjectRegistry, ProjectRoutingError
from factory.orchestration.transitions import TaskLifecycleService


class FeedbackReconciliationService:
    """Advance only exact, verified deliveries; retry partial provider writes safely."""

    def __init__(
        self,
        tasks: TaskRepository,
        pull_requests: PullRequestRepository,
        evidence: DeliveryEvidenceSource,
        issues: IssueCompletionSink,
        cards: WorkItemFeedbackSink,
        events: FeedbackEventRepository,
        links: BacklogLinkRepository,
        registry: ProjectRegistry,
        sprints: SprintRepository | None = None,
    ) -> None:
        self._tasks = tasks
        self._prs = pull_requests
        self._evidence = evidence
        self._issues = issues
        self._cards = cards
        self._events = events
        self._links = links
        self._registry = registry
        self._sprints = sprints
        self._lifecycle = TaskLifecycleService(tasks)

    def reconcile(self, task: FactoryTask, run: AgentRun) -> bool:
        """Return true only after the final Issue and card state is confirmed."""
        task = self._tasks.get(task.task_id) or task
        identity = self._identity(task, run)
        if identity is None or run.status is not RunStatus.SUCCEEDED:
            return False
        facts = self._evidence.evidence(identity)
        if facts.complete:
            if task.status in {TaskStatus.WAITING_HUMAN, TaskStatus.VALIDATING}:
                task = self._lifecycle.transition(task.task_id, TaskStatus.DONE)
            if task.status is not TaskStatus.DONE:
                return False
            self._issues.complete(identity)
            self._cards.sync(identity, "DONE")
            self._events.record_completed(identity)
            return True
        if task.status is TaskStatus.DONE:
            return False
        self._cards.sync(identity, "MERGED" if facts.merged else task.status.value)
        return False

    def sync(self, task: FactoryTask, run: AgentRun, phase: str) -> None:
        identity = self._identity(task, run)
        if identity is not None:
            self._cards.sync(identity, phase)

    def _identity(self, task: FactoryTask, run: AgentRun) -> FeedbackIdentity | None:
        source = task.source
        workspace = run.workspace
        if (
            task.kind is not TaskKind.CODE
            or source is None
            or source.provider != "github"
            or source.repository_slug != task.target_repository
            or run.task_id != task.task_id
            or run.project_id != task.project_id
            or workspace is None
            or workspace.repository_slug != task.target_repository
        ):
            return None
        try:
            profile = self._registry.resolve(task.project_id, task.target_repository)
        except ProjectRoutingError:
            return None
        pr = self._prs.find_by_branch(task.target_repository, workspace.branch)
        if not self._matches_pr(task, workspace.branch, pr, profile.base_ref):
            return None
        assert pr is not None and pr.number is not None and pr.commit_sha is not None
        link = self._links.find_work_item(
            task.project_id, source.repository_slug, source.issue_number
        )
        if link is None or link[0] != "trello":
            return None
        sprint_id = (
            self._sprints.find_for_work_item(task.project_id, link[0], link[1])
            if self._sprints is not None
            else None
        )
        return FeedbackIdentity(
            project_id=task.project_id,
            repository_slug=task.target_repository,
            issue_number=source.issue_number,
            task_id=task.task_id,
            run_id=run.run_id,
            workspace_id=workspace.workspace_id,
            commit_sha=pr.commit_sha,
            pull_request_number=pr.number,
            sprint_id=sprint_id,
            work_item_provider=link[0],
            work_item_id=link[1],
        )

    @staticmethod
    def _matches_pr(task: FactoryTask, branch: str, pr: PullRequest | None, base: str) -> bool:
        return bool(
            pr is not None
            and pr.task_id == task.task_id
            and pr.repository_slug == task.target_repository
            and pr.base_branch == base
            and pr.head_branch == branch
            and pr.number is not None
            and pr.commit_sha
            and pr.run_id is not None
        )
