"""Durable evidence for deterministic post-rebase revalidation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from factory.domain.models import QualityGate
from factory.domain.security import SecurityReview


def _now() -> datetime:
    return datetime.now(UTC)


class RevalidationResult(StrEnum):
    """Lifecycle of one infrastructure-owned exact-head revalidation attempt."""

    PENDING = "PENDING"
    WAITING_CI = "WAITING_CI"
    PASSED = "PASSED"
    FAILED = "FAILED"


class ExactHeadCiStatus(StrEnum):
    """Provider CI state for one immutable commit SHA."""

    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class WorkspaceRevisionSnapshot:
    """Immutable Git identities for one clean workspace checkout."""

    head_sha: str
    tree_sha: str

    def __post_init__(self) -> None:
        if not self.head_sha.strip() or not self.tree_sha.strip():
            raise ValueError("workspace revision identity is required")


@dataclass(frozen=True, slots=True)
class ExactHeadCiEvidence:
    """Required provider CI observed for exactly one commit SHA."""

    sha: str
    status: ExactHeadCiStatus
    checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.sha.strip():
            raise ValueError("CI SHA is required")


@dataclass(slots=True)
class PostRebaseRevalidation:
    """New evidence after an external/deterministic rebase of an existing PR.

    ``source_run_id`` is historical provenance only. This record never modifies
    that run. The mutable publication identity lives on the PR and is rebound
    only after this attempt reaches exact-head CI success.
    """

    task_id: str
    source_run_id: str
    repository_slug: str
    head_branch: str
    pull_request_number: int
    previous_head: str
    new_head: str
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    validated_tree: str | None = None
    gates: tuple[QualityGate, ...] = ()
    security_review: SecurityReview | None = None
    ci_sha: str | None = None
    ci_checks: tuple[str, ...] = ()
    result: RevalidationResult = RevalidationResult.PENDING
    failure_reason: str | None = None
    started_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None

    @property
    def is_terminal(self) -> bool:
        return self.result in {RevalidationResult.PASSED, RevalidationResult.FAILED}
