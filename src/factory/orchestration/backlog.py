"""Provider-neutral, fail-closed backlog issue materialization."""

from __future__ import annotations

from dataclasses import dataclass

from factory.domain.backlog import MaterializedIssue, WorkItem
from factory.domain.ports import BacklogLinkRepository, BacklogSink, BacklogSource
from factory.domain.projects import ProjectRegistry, ProjectRoutingError


@dataclass(frozen=True, slots=True)
class BacklogSummary:
    examined: int = 0
    created: int = 0
    existing: int = 0
    ineligible: int = 0
    uncertain: int = 0


class BacklogMaterializationService:
    """Reconcile backlog items into Issues without dispatching or importing providers."""

    def __init__(
        self,
        source: BacklogSource,
        sink: BacklogSink,
        links: BacklogLinkRepository,
        registry: ProjectRegistry | None = None,
    ) -> None:
        self._source = source
        self._sink = sink
        self._links = links
        self._registry = registry

    def reconcile(self, external_id: str | None = None) -> BacklogSummary:
        """Process one event-selected item or a full periodic backlog snapshot.

        An uncertain remote write is looked up on every retry, but is never
        blindly repeated: a timeout may have occurred after GitHub created it.
        """
        items = (
            [self._source.get_item(external_id)]
            if external_id is not None
            else self._source.list_items()
        )
        created = existing = ineligible = uncertain = 0
        for candidate in items:
            if self._registry is not None:
                try:
                    self._registry.resolve(candidate.project_id, candidate.target_repository)
                except ProjectRoutingError:
                    self._links.record_rejection(candidate, "PROJECT_ROUTING_REJECTED")
                    ineligible += 1
                    continue
            if not candidate.eligible or not candidate.dependencies_satisfied:
                ineligible += 1
                continue
            self._links.reserve(candidate)
            linked = self._links.get_issue(candidate.provider, candidate.external_id)
            if linked is not None:
                existing += 1
                continue
            # A remote issue may exist even when its link transaction did not
            # commit. Search before every possible create.
            found = self._sink.find_issue(candidate)
            if found is not None:
                self._links.complete(candidate, found)
                existing += 1
                continue
            current = self._source.get_item(candidate.external_id)
            if self._registry is not None:
                try:
                    self._registry.resolve(current.project_id, current.target_repository)
                except ProjectRoutingError:
                    self._links.record_rejection(current, "PROJECT_ROUTING_REJECTED")
                    ineligible += 1
                    continue
            if not _still_eligible(candidate, current):
                ineligible += 1
                continue
            if not self._links.begin_write(candidate):
                uncertain += 1
                continue
            # Once this call begins, a failure is ambiguous. The durable
            # POSTING state suppresses another POST until lookup recovers it.
            issue: MaterializedIssue = self._sink.create_issue(current)
            self._links.complete(candidate, issue)
            created += 1
        return BacklogSummary(len(items), created, existing, ineligible, uncertain)


def _still_eligible(candidate: WorkItem, current: WorkItem) -> bool:
    return (
        current.provider == candidate.provider
        and current.external_id == candidate.external_id
        and current.target_repository == candidate.target_repository
        and current.project_id == candidate.project_id
        and current.eligible
        and current.dependencies_satisfied
    )
