"""Domain-level errors.

Errors that carry meaning about the domain model itself live here so that every
layer can raise or catch them without importing a higher layer. They must never
carry credentials or raw external payloads.
"""

from __future__ import annotations

from factory.domain.enums import TaskStatus
from factory.domain.models import TaskSource


class FactoryError(Exception):
    """Base class for every error the factory raises deliberately."""


class PersistenceError(FactoryError):
    """A persistence operation failed for a reason other than a known conflict."""


class TaskSourceError(FactoryError, ValueError):
    """A task's source identity is missing or malformed for the operation."""


class DuplicateTaskError(FactoryError):
    """A task with the same structured source identity already exists.

    Raised by persistence as a defense-in-depth signal: the uniqueness of
    ``(provider, repository_slug, issue_number)`` is enforced by the storage
    layer, not only by application-level checks.
    """

    def __init__(self, source: TaskSource) -> None:
        super().__init__(
            f"task already exists for source {source.provider}:"
            f"{source.repository_slug}#{source.issue_number}"
        )
        self.source = source


class TaskStateChangedError(FactoryError):
    """A stored task was not in the status a transition expected.

    Signals a lost update: the caller validated ``expected -> target`` against a
    snapshot that no longer matches storage, so the atomic write is refused
    rather than silently applied to a different starting state.
    """

    def __init__(self, task_id: str, expected: TaskStatus, actual: TaskStatus) -> None:
        super().__init__(
            f"task {task_id} status changed: expected {expected.value}, found {actual.value}"
        )
        self.task_id = task_id
        self.expected = expected
        self.actual = actual


__all__ = [
    "DuplicateTaskError",
    "FactoryError",
    "PersistenceError",
    "TaskSourceError",
    "TaskStateChangedError",
]
