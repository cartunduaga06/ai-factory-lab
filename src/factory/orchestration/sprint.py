"""Authorized, ordered selection over E2 and the existing factory runtime."""

from __future__ import annotations

from dataclasses import dataclass

from factory.domain.backlog import WorkItem
from factory.domain.enums import TaskStatus
from factory.domain.models import FactoryTask, TaskSource
from factory.domain.ports import (
    BacklogLinkRepository,
    BacklogSource,
    FeedbackEventRepository,
    SprintRepository,
    TaskRepository,
)
from factory.domain.projects import ProjectRegistry, ProjectRoutingError
from factory.domain.sprint import SprintManifest, SprintState, SprintStep
from factory.orchestration.backlog import BacklogMaterializationService


@dataclass(frozen=True, slots=True)
class SprintPlanRow:
    position: int
    external_id: str
    eligible: bool
    blockers: tuple[str, ...]


class AuthorizedBacklogSource(BacklogSource):
    """Protect the immutable snapshot across E2's repeated provider reads."""

    def __init__(self, source: BacklogSource, sprints: SprintRepository) -> None:
        self._source = source
        self._sprints = sprints

    def list_items(self) -> tuple[WorkItem, ...]:
        current = self._sprints.current()
        if current is None or current[1] is not SprintState.ACTIVE:
            return ()
        manifest, _, position = current
        return (
            (self.get_item(manifest.steps[position].item.external_id),)
            if position < len(manifest.steps)
            else ()
        )

    def get_item(self, external_id: str) -> WorkItem:
        current = self._sprints.current()
        if current is None or current[1] is not SprintState.ACTIVE:
            raise ValueError("sprint is not active")
        manifest, _, position = current
        if position >= len(manifest.steps):
            raise ValueError("sprint is complete")
        snapshot = manifest.steps[position].item
        if external_id != snapshot.external_id:
            raise ValueError("work item is outside the authorized sprint position")
        actual = self._source.get_item(external_id)
        if actual != snapshot:
            raise ValueError("authorized work item snapshot changed")
        return actual


class DependencyResolver:
    """Resolve named predecessors against durable task outcomes, not position alone."""

    def __init__(self, links: BacklogLinkRepository, tasks: TaskRepository) -> None:
        self._links = links
        self._tasks = tasks

    def complete(self, manifest: SprintManifest, position: int) -> bool:
        step = manifest.steps[position]
        if not step.item.dependencies_satisfied:
            return False
        preceding = {
            f"{prior.item.provider}:{prior.item.external_id}": prior.item
            for prior in manifest.steps[:position]
        }
        for dependency in step.dependencies:
            item = preceding.get(dependency)
            if item is None:
                return False
            issue = self._links.get_issue(item.provider, item.external_id)
            if issue is None:
                return False
            task = self._tasks.find_by_source(
                TaskSource("github", issue.repository_slug, issue.number)
            )
            if task is None or task.status is not TaskStatus.DONE:
                return False
        return True


class SprintService:
    """Validate and advance a WIP=1 sprint without executing a second runtime."""

    def __init__(
        self,
        source: BacklogSource,
        materializer: BacklogMaterializationService,
        links: BacklogLinkRepository,
        tasks: TaskRepository,
        sprints: SprintRepository,
        registry: ProjectRegistry | None = None,
        feedback_events: FeedbackEventRepository | None = None,
    ) -> None:
        self._source = source
        self._materializer = materializer
        self._links = links
        self._tasks = tasks
        self._sprints = sprints
        self._registry = registry
        self._feedback_events = feedback_events
        self._dependencies = DependencyResolver(links, tasks)

    def draft(
        self, sprint_id: str, entries: tuple[tuple[str, tuple[str, ...]], ...]
    ) -> SprintManifest:
        """Read current provider snapshots without storing or creating anything."""
        steps = tuple(
            SprintStep(self._source.get_item(external_id), dependencies)
            for external_id, dependencies in entries
        )
        return SprintManifest(sprint_id, steps)

    def plan(self, manifest: SprintManifest) -> tuple[SprintPlanRow, ...]:
        """Describe eligibility and blockers without writes or provider mutation."""
        rows: list[SprintPlanRow] = []
        current = self._sprints.current()
        live_other = (
            current is not None
            and current[1] in {SprintState.ACTIVE, SprintState.PAUSED}
            and current[0].sprint_id != manifest.sprint_id
        )
        pending_human = bool(self._tasks.list(TaskStatus.WAITING_HUMAN))
        busy = any(
            self._tasks.list(status)
            for status in (
                TaskStatus.CLAIMED,
                TaskStatus.RUNNING,
                TaskStatus.VALIDATING,
                TaskStatus.PR_OPEN,
            )
        )
        scope = (manifest.steps[0].item.provider, manifest.steps[0].item.target_repository)
        seen: set[str] = set()
        for position, step in enumerate(manifest.steps):
            blockers: list[str] = []
            key = f"{step.item.provider}:{step.item.external_id}"
            if key in seen:
                blockers.append("duplicate_work_item")
            if (
                self._registry is None
                and (step.item.provider, step.item.target_repository) != scope
            ):
                blockers.append("outside_scope")
            if self._registry is not None:
                try:
                    self._registry.resolve(step.item.project_id, step.item.target_repository)
                except ProjectRoutingError:
                    blockers.append("project_routing_rejected")
            if any(dependency not in seen for dependency in step.dependencies):
                blockers.append("dependency_not_preceding")
            seen.add(key)
            if live_other:
                blockers.append("another_sprint_active")
            if pending_human:
                blockers.append("pending_human_gate")
            if busy:
                blockers.append("wip_busy")
            if not step.item.eligible or not step.item.dependencies_satisfied:
                blockers.append("source_ineligible")
            if self._source.get_item(step.item.external_id) != step.item:
                blockers.append("snapshot_changed")
            if position:
                blockers.append("ordered_predecessor_waiting")
            rows.append(
                SprintPlanRow(position, step.item.external_id, not blockers, tuple(blockers))
            )
        return tuple(rows)

    def authorize(self, manifest: SprintManifest) -> None:
        current = self._sprints.current()
        if current is not None and current[0].sprint_id == manifest.sprint_id:
            self._sprints.authorize(manifest)
            return
        rows = self.plan(manifest)
        for step, row in zip(manifest.steps, rows, strict=True):
            if "project_routing_rejected" in row.blockers:
                self._links.record_rejection(step.item, "PROJECT_ROUTING_REJECTED")
        if any(
            blocker != "ordered_predecessor_waiting" for row in rows for blocker in row.blockers
        ):
            raise ValueError("sprint plan has blockers")
        self._sprints.authorize(manifest)

    def prepare(self) -> bool:
        """Select only the current authorized step and materialize via E2."""
        current = self._sprints.current()
        if current is None or current[1] is not SprintState.ACTIVE:
            return False
        manifest, _, position = current
        while position < len(manifest.steps):
            item = manifest.steps[position].item
            issue = self._links.get_issue(item.provider, item.external_id)
            task = self._task_for(item) if issue is not None else None
            if task is not None and task.status is TaskStatus.DONE:
                if self._feedback_events is not None and not self._feedback_events.is_completed(
                    task.task_id
                ):
                    self._sprints.move(
                        manifest.sprint_id, SprintState.PAUSED, position, "SprintPaused"
                    )
                    return False
                position += 1
                state = (
                    SprintState.COMPLETE if position == len(manifest.steps) else SprintState.ACTIVE
                )
                self._sprints.move(
                    manifest.sprint_id,
                    state,
                    position,
                    "SprintCompleted" if state is SprintState.COMPLETE else "SprintAdvanced",
                )
                continue
            if task is not None and task.status in {
                TaskStatus.WAITING_HUMAN,
                TaskStatus.BLOCKED,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                self._sprints.move(manifest.sprint_id, SprintState.PAUSED, position, "SprintPaused")
                return False
            if self._source.get_item(item.external_id) != item:
                self._sprints.move(manifest.sprint_id, SprintState.PAUSED, position, "SprintPaused")
                return False
            if not self._dependencies.complete(manifest, position):
                self._sprints.move(manifest.sprint_id, SprintState.PAUSED, position, "SprintPaused")
                return False
            if issue is None:
                self._materializer.reconcile(item.external_id)
                issue = self._links.get_issue(item.provider, item.external_id)
            if issue is not None:
                self._sprints.move(
                    manifest.sprint_id, SprintState.ACTIVE, position, "WorkItemSelected"
                )
            return issue is not None
        return False

    def allows(self, task: FactoryTask) -> bool:
        current = self._sprints.current()
        if current is None or current[1] is not SprintState.ACTIVE:
            return False
        return self._matches_current(task, current)

    def allows_review(self, task: FactoryTask) -> bool:
        """Let a paused sprint observe its own PR decision without dispatch."""
        current = self._sprints.current()
        if current is None or current[1] not in {SprintState.ACTIVE, SprintState.PAUSED}:
            return False
        return self._matches_current(task, current)

    def _matches_current(
        self, task: FactoryTask, current: tuple[SprintManifest, SprintState, int]
    ) -> bool:
        manifest, _, position = current
        if position >= len(manifest.steps) or task.source is None:
            return False
        issue = self._links.get_issue(
            manifest.steps[position].item.provider, manifest.steps[position].item.external_id
        )
        return (
            issue is not None
            and task.source.provider == "github"
            and task.source.repository_slug == issue.repository_slug
            and task.source.issue_number == issue.number
        )

    def observe(self, task: FactoryTask | None) -> None:
        if task is None or not self.allows(task):
            return
        current = self._sprints.current()
        assert current is not None
        manifest, _, position = current
        if task.status in {
            TaskStatus.WAITING_HUMAN,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            self._sprints.move(manifest.sprint_id, SprintState.PAUSED, position, "SprintPaused")

    def resume(self, sprint_id: str) -> None:
        current = self._sprints.current()
        if (
            current is None
            or current[0].sprint_id != sprint_id
            or current[1] is not SprintState.PAUSED
        ):
            raise ValueError("sprint is not paused")
        manifest, _, position = current
        item = manifest.steps[position].item
        task = self._task_for(item)
        if task is not None and task.status in {
            TaskStatus.WAITING_HUMAN,
            TaskStatus.BLOCKED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }:
            raise ValueError("human gate remains unresolved")
        if self._source.get_item(item.external_id) != item or not self._dependencies.complete(
            manifest, position
        ):
            raise ValueError("sprint dependencies remain unresolved")
        self._sprints.move(sprint_id, SprintState.ACTIVE, position, "SprintResumed")

    def resume_completed(self) -> None:
        """Release the human gate after verified completion of the current step."""
        current = self._sprints.current()
        if current is None or current[1] is not SprintState.PAUSED:
            return
        manifest, _, position = current
        if position < len(manifest.steps):
            task = self._task_for(manifest.steps[position].item)
            if (
                task is not None
                and task.status is TaskStatus.DONE
                and (
                    self._feedback_events is None
                    or self._feedback_events.is_completed(task.task_id)
                )
            ):
                self._sprints.move(
                    manifest.sprint_id, SprintState.ACTIVE, position, "SprintResumed"
                )

    def cancel(self, sprint_id: str) -> None:
        current = self._sprints.current()
        if (
            current is None
            or current[0].sprint_id != sprint_id
            or current[1] not in {SprintState.ACTIVE, SprintState.PAUSED}
        ):
            raise ValueError("sprint is not live")
        self._sprints.move(sprint_id, SprintState.CANCELLED, current[2], "SprintCancelled")

    def pause(self, sprint_id: str) -> None:
        current = self._sprints.current()
        if (
            current is None
            or current[0].sprint_id != sprint_id
            or current[1] is not SprintState.ACTIVE
        ):
            raise ValueError("sprint is not active")
        self._sprints.move(sprint_id, SprintState.PAUSED, current[2], "SprintPaused")

    def is_paused(self) -> bool:
        current = self._sprints.current()
        return current is not None and current[1] is SprintState.PAUSED

    def _task_for(self, item: WorkItem) -> FactoryTask | None:
        issue = self._links.get_issue(item.provider, item.external_id)
        if issue is None:
            return None
        return self._tasks.find_by_source(TaskSource("github", issue.repository_slug, issue.number))
