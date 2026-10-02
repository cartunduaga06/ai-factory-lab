"""Bounded failure decisions based only on durable, sanitized run facts."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from factory.domain.enums import RunStatus, TaskStatus, ValidationOutcome
from factory.domain.models import AgentRun, FactoryTask


class FailureClass(StrEnum):
    """Whether another observation or a new coding attempt is permitted."""

    TRANSIENT = "TRANSIENT"
    CORRECTABLE = "CORRECTABLE"
    NON_RECOVERABLE = "NON_RECOVERABLE"


@dataclass(frozen=True, slots=True)
class RecoveryPolicy:
    """Cap automatic QA attempts and use deterministic exponential delay."""

    correction_limit: int = 2
    run_retry_limit: int = 3
    base_backoff_seconds: float = 60.0
    max_backoff_seconds: float = 300.0

    def __post_init__(self) -> None:
        if (
            self.correction_limit < 0
            or self.run_retry_limit < 1
            or self.base_backoff_seconds < 0
            or self.max_backoff_seconds < self.base_backoff_seconds
        ):
            raise ValueError("invalid recovery policy")

    @staticmethod
    def classify(run: AgentRun) -> FailureClass:
        """A failed QA gate is correctable; uncertain active runs are observed only."""
        if run.validation_outcome is ValidationOutcome.GATES_FAILED:
            return FailureClass.CORRECTABLE
        if run.status in {RunStatus.PENDING, RunStatus.RUNNING}:
            return FailureClass.TRANSIENT
        return FailureClass.NON_RECOVERABLE

    @staticmethod
    def classify_retry(
        task: FactoryTask, latest: AgentRun | None, *, rework_claim: bool = False
    ) -> FailureClass:
        """Only a blocked or legacy claimed attempt can be retried explicitly."""
        if task.status not in {TaskStatus.BLOCKED, TaskStatus.CLAIMED}:
            return FailureClass.NON_RECOVERABLE
        if latest is None:
            return FailureClass.TRANSIENT
        if latest.status is RunStatus.FAILED:
            return FailureClass.CORRECTABLE
        if (
            task.status is TaskStatus.BLOCKED
            and latest.is_terminal
            and latest.validation_outcome is ValidationOutcome.GATES_FAILED
        ):
            return FailureClass.CORRECTABLE
        if RecoveryPolicy.is_publication_retry(task, latest):
            return FailureClass.CORRECTABLE
        if task.status is TaskStatus.CLAIMED and rework_claim and latest.is_terminal:
            return FailureClass.CORRECTABLE
        return FailureClass.NON_RECOVERABLE

    @staticmethod
    def is_publication_retry(task: FactoryTask, latest: AgentRun | None) -> bool:
        """Bind operator recovery to the exact validated run and isolated workspace."""
        return bool(
            task.status is TaskStatus.BLOCKED
            and latest is not None
            and latest.task_id == task.task_id
            and latest.status is RunStatus.SUCCEEDED
            and latest.validation_outcome is ValidationOutcome.READY_FOR_NEXT_PHASE
            and latest.validated_revision
            and latest.validated_revision.strip()
            and latest.project_id == task.project_id
            and latest.workspace is not None
            and latest.workspace.repository_slug == task.target_repository
            and task.blocked_reason
            == f"publication failed: run {latest.run_id}, workspace {latest.workspace.workspace_id}"
        )

    def delay_for(self, correction_number: int) -> float:
        """Return the bounded delay before correction number 1, 2, and so on."""
        if correction_number < 1:
            raise ValueError("correction number must be positive")
        factor: float = 2.0 ** min(correction_number - 1, 30)
        return min(
            self.max_backoff_seconds,
            self.base_backoff_seconds * factor,
        )
