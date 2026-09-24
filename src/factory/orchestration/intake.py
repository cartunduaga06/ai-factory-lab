"""Provider-agnostic issue intake.

Turns eligible work items from any :class:`~factory.domain.ports.IssueSource`
into persisted :class:`~factory.domain.models.FactoryTask` records.

The service depends only on the two ports, so it is identical for GitHub today
and any future source. It deliberately imports neither the concrete GitHub
adapter nor SQLite: swapping either changes nothing here.

Idempotency is the core guarantee. A task is identified by its structured
:class:`~factory.domain.models.TaskSource` (provider + repository + issue
number), and intake persists a task only when no task with that source exists —
checked before writing and enforced again by a uniqueness constraint in storage.
Running intake repeatedly over the same issues therefore creates zero duplicates
and leaves existing tasks untouched.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from factory.domain.models import Repository
from factory.domain.ports import IssueSource, TaskRepository

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class IntakeSummary:
    """Outcome of one intake run.

    ``errors`` counts issues that could not be persisted. Intake continues past
    a failure rather than aborting the whole run.
    """

    discovered: int = 0
    created: int = 0
    existing: int = 0
    errors: int = 0


class IssueIntakeService:
    """Discovers eligible issues and persists the ones that are new."""

    def __init__(self, source: IssueSource, repository: TaskRepository) -> None:
        self._source = source
        self._repository = repository

    def intake(self, repository: Repository) -> IntakeSummary:
        """Run one intake pass for ``repository``.

        Persistence is assumed to be initialized already; the caller owns that
        lifecycle so this service can stay free of storage setup.
        """
        discovered = list(self._source.list_open_tasks(repository))
        created = 0
        existing = 0
        errors = 0

        for task in discovered:
            if task.source is None:
                # A source that yields an unidentified task cannot be made
                # idempotent, so it is refused rather than guessed at.
                errors += 1
                logger.warning("refusing task without structured source identity")
                continue
            if self._repository.find_by_source(task.source) is not None:
                existing += 1
                continue
            try:
                self._repository.save(task)
            except Exception:  # noqa: BLE001 - one bad issue must not stop intake
                # Captures DuplicateTaskError from a concurrent writer too: the
                # storage constraint, not the pre-check, is the real guard.
                # Only the reference is logged — never the issue body.
                errors += 1
                logger.warning("failed to persist task %s", task.source.external_ref)
                continue
            created += 1

        return IntakeSummary(
            discovered=len(discovered),
            created=created,
            existing=existing,
            errors=errors,
        )


__all__ = ["IntakeSummary", "IssueIntakeService"]
