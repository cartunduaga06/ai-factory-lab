"""Post-review reconciliation across a project Issue, task, PR and WorkItem."""

from __future__ import annotations

import re

from factory.domain.enums import RunStatus, TaskKind, TaskStatus
from factory.domain.feedback import FeedbackIdentity
from factory.domain.models import AgentRun, FactoryTask, PullRequest
from factory.domain.ports import (
    BacklogLinkRepository,
    DeliveryEvidenceSource,
    FeedbackEventRepository,
    IssueCompletionSink,
    PullRequestRepository,
    RunRepository,
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
        runs: RunRepository,
        pull_requests: PullRequestRepository,
        evidence: DeliveryEvidenceSource,
        issues: IssueCompletionSink,
        cards: WorkItemFeedbackSink | None,
        events: FeedbackEventRepository,
        links: BacklogLinkRepository,
        registry: ProjectRegistry,
        sprints: SprintRepository | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
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
        matched = self._identity(task, run)
        if matched is None or run.status is not RunStatus.SUCCEEDED:
            return False
        identity, pr = matched
        facts = self._evidence.evidence(identity)
        if facts.merged:
            self._prs.record_merged(pr)
        if facts.complete:
            if task.status in {TaskStatus.WAITING_HUMAN, TaskStatus.VALIDATING}:
                task = self._lifecycle.transition(task.task_id, TaskStatus.DONE)
            if task.status is not TaskStatus.DONE:
                return False
            self._issues.complete(identity)
            if identity.work_item_provider == "trello" and self._cards is not None:
                self._cards.sync(identity, "DONE")
            self._events.record_completed(identity)
            return True
        if task.status is TaskStatus.DONE:
            return False
        if identity.work_item_provider == "trello" and self._cards is not None:
            self._cards.sync(identity, "MERGED" if facts.merged else task.status.value)
        return False

    def reconcile_external(self, task: FactoryTask, run: AgentRun) -> bool:
        """Reconcile explicit provider closures and merged PRs without unsafe run changes."""
        task = self._tasks.get(task.task_id) or task
        if run.status is not RunStatus.SUCCEEDED:
            return False
        matched = self._identity(task, run)
        if matched is None or task.status in {
            TaskStatus.DISCOVERED,
            TaskStatus.READY,
            TaskStatus.CLAIMED,
            TaskStatus.RUNNING,
            TaskStatus.PR_OPEN,
            TaskStatus.CHANGES_REQUESTED,
            TaskStatus.BLOCKED,
        }:
            return False
        identity, pr = matched
        try:
            issue_state, reason = self._issues.state(
                identity.repository_slug, identity.issue_number
            )
        except Exception:
            issue_state, reason = "unknown", None
        facts = self._evidence.evidence(identity)
        superseded = _has_superseded_marker(task.body) or _has_superseded_marker(task.title)
        completed_issue = issue_state == "closed" and reason in {
            "completed",
            "not_planned",
            "duplicate",
        }
        merge_resolution = facts.merged and facts.complete
        if facts.merged and task.status is TaskStatus.WAITING_HUMAN:
            # Persist provider merge immediately; task completion still waits for
            # CI, integration and deployment evidence in the delivery path.
            self._prs.record_merged(pr)
        if not merge_resolution and not completed_issue and not superseded:
            return False
        if facts.merged and merge_resolution:
            self._prs.record_merged(pr)
        if task.status is not TaskStatus.DONE:
            if task.status not in {
                TaskStatus.WAITING_HUMAN,
                TaskStatus.VALIDATING,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return False
            task = self._resolve_done(task)
        task = self._tasks.get(task.task_id) or task
        if task.blocked_reason is not None:
            task.blocked_reason = None
            self._tasks.update(task)
        if facts.complete and issue_state != "closed":
            self._issues.complete(identity)
        elif superseded and issue_state != "closed":
            self._issues.close(identity, "not_planned")
        if identity.work_item_provider == "trello" and self._cards is not None:
            self._cards.sync(identity, "DONE")
        resolution = reason if completed_issue else ("not_planned" if superseded else "completed")
        if facts.complete or superseded or completed_issue:
            self._events.record_resolution(identity, resolution or "completed")
        return facts.complete or superseded or completed_issue

    def reconcile_provider_closure(self, task: FactoryTask, run: AgentRun) -> bool:
        """Apply a completed Issue closure even when delivery CI policy is stricter."""
        task = self._tasks.get(task.task_id) or task
        if run.status is not RunStatus.SUCCEEDED:
            return False
        stored_run = self._runs.get_run(run.run_id)
        if stored_run is None or stored_run.status is not RunStatus.SUCCEEDED:
            return False
        matched = self._identity(task, run)
        if matched is None or task.status in {
            TaskStatus.DISCOVERED,
            TaskStatus.READY,
            TaskStatus.CLAIMED,
            TaskStatus.RUNNING,
            TaskStatus.PR_OPEN,
            TaskStatus.CHANGES_REQUESTED,
            TaskStatus.BLOCKED,
        }:
            return False
        identity, _ = matched
        try:
            state, reason = self._issues.state(identity.repository_slug, identity.issue_number)
        except Exception:
            return False
        if state != "closed" or reason not in {"completed", "not_planned", "duplicate"}:
            return False
        if task.status is not TaskStatus.DONE:
            if task.status not in {
                TaskStatus.WAITING_HUMAN,
                TaskStatus.VALIDATING,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                return False
            task = self._resolve_done(task)
        task = self._tasks.get(task.task_id) or task
        if task.blocked_reason is not None:
            task.blocked_reason = None
            self._tasks.update(task)
        if identity.work_item_provider == "trello" and self._cards is not None:
            self._cards.sync(identity, "DONE")
        self._events.record_resolution(identity, reason)
        return True

    def sync(self, task: FactoryTask, run: AgentRun, phase: str) -> None:
        matched = self._identity(task, run)
        if (
            matched is not None
            and matched[0].work_item_provider == "trello"
            and self._cards is not None
        ):
            self._cards.sync(matched[0], phase)

    def _resolve_done(self, task: FactoryTask) -> FactoryTask:
        if task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
            return self._lifecycle.reconcile_terminal_resolution(task.task_id, task.status)
        if task.status is TaskStatus.DONE:
            return task
        return self._lifecycle.transition(task.task_id, TaskStatus.DONE)

    def is_github_direct(self, task: FactoryTask) -> bool:
        """Allow a persisted direct task to bypass only Sprint review routing."""
        return self._links.reconciliation_origin(task.task_id) == ("github-direct", None)

    def _identity(
        self, task: FactoryTask, run: AgentRun
    ) -> tuple[FeedbackIdentity, PullRequest] | None:
        source = task.source
        workspace = run.workspace
        stored_run = self._runs.get_run(run.run_id)
        if (
            task.kind is not TaskKind.CODE
            or source is None
            or source.provider != "github"
            or source.repository_slug != task.target_repository
            or run.task_id != task.task_id
            or run.project_id != task.project_id
            or workspace is None
            or workspace.repository_slug != task.target_repository
            or stored_run is None
            or stored_run.task_id != task.task_id
            or stored_run.project_id != task.project_id
            or stored_run.workspace != workspace
            or stored_run.status is not RunStatus.SUCCEEDED
            or not self._is_latest_run(task.task_id, run.run_id)
        ):
            return None
        try:
            profile = self._registry.resolve(task.project_id, task.target_repository)
        except ProjectRoutingError:
            return None
        pr = self._prs.find_by_branch(task.target_repository, workspace.branch)
        if not self._matches_pr(task, run, workspace.branch, pr, profile.base_ref):
            return None
        assert pr is not None and pr.number is not None and pr.commit_sha is not None
        origin = self._links.reconciliation_origin(task.task_id)
        if origin is None:
            return None
        if origin[0] == "trello" and self._cards is None:
            return None
        link = self._links.find_work_item(
            task.project_id, source.repository_slug, source.issue_number
        )
        if origin[0] == "trello" and link != origin:
            return None
        if origin[0] == "github-direct" and link is not None:
            return None
        sprint_id = (
            self._sprints.find_for_work_item(task.project_id, link[0], link[1])
            if self._sprints is not None and link is not None
            else None
        )
        identity = FeedbackIdentity(
            project_id=task.project_id,
            repository_slug=task.target_repository,
            issue_number=source.issue_number,
            task_id=task.task_id,
            run_id=run.run_id,
            workspace_id=workspace.workspace_id,
            commit_sha=pr.commit_sha,
            pull_request_number=pr.number,
            branch=workspace.branch,
            sprint_id=sprint_id,
            work_item_provider=link[0] if link is not None else None,
            work_item_id=link[1] if link is not None else None,
        )
        return identity, pr

    @staticmethod
    def _matches_pr(
        task: FactoryTask, run: AgentRun, branch: str, pr: PullRequest | None, base: str
    ) -> bool:
        return bool(
            pr is not None
            and pr.task_id == task.task_id
            and pr.repository_slug == task.target_repository
            and pr.base_branch == base
            and pr.head_branch == branch
            and pr.number is not None
            and pr.commit_sha
            and pr.run_id == run.run_id
        )

    def _is_latest_run(self, task_id: str, run_id: str) -> bool:
        runs = self._runs.list_runs(task_id)
        return bool(runs) and runs[-1].run_id == run_id


def _has_superseded_marker(value: str) -> bool:
    """Accept only an explicit standalone marker, not incidental prose."""
    return (
        re.search(
            r"(?im)^\s*(?:<!--\s*)?factory-resolution:\s*superseded\s*(?:-->)?\s*$",
            value,
        )
        is not None
    )
