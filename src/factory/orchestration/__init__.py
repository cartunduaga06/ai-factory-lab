"""Orchestration layer: task lifecycle and agent dispatch.

This layer coordinates work but depends only on the ``AgentAdapter`` protocol
declared in the domain. It must never import a concrete agent engine directly.
"""

from factory.orchestration.intake import IntakeSummary, IssueIntakeService
from factory.orchestration.lifecycle import (
    TERMINAL_STATES,
    TRANSITIONS,
    can_transition,
    next_states,
)
from factory.orchestration.machine import InvalidTransitionError, TaskStateMachine
from factory.orchestration.transitions import TaskLifecycleService

__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "IntakeSummary",
    "InvalidTransitionError",
    "IssueIntakeService",
    "TaskLifecycleService",
    "TaskStateMachine",
    "can_transition",
    "next_states",
]
