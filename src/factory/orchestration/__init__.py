"""Orchestration layer: task lifecycle and agent dispatch.

This layer coordinates work but depends only on the ``AgentAdapter`` protocol
declared in the domain. It must never import a concrete agent engine directly.
"""

from factory.orchestration.dispatch import DispatchService, branch_for
from factory.orchestration.intake import IntakeSummary, IssueIntakeService
from factory.orchestration.lifecycle import (
    TERMINAL_STATES,
    TRANSITIONS,
    can_transition,
    next_states,
)
from factory.orchestration.machine import InvalidTransitionError, TaskStateMachine
from factory.orchestration.publication import (
    PublicationResult,
    PublicationService,
    build_pull_request_body,
)
from factory.orchestration.tracking import RunRefresh, RunTrackingService
from factory.orchestration.transitions import TaskLifecycleService

__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "DispatchService",
    "IntakeSummary",
    "InvalidTransitionError",
    "IssueIntakeService",
    "PublicationResult",
    "PublicationService",
    "RunRefresh",
    "RunTrackingService",
    "TaskLifecycleService",
    "TaskStateMachine",
    "branch_for",
    "build_pull_request_body",
    "can_transition",
    "next_states",
]
