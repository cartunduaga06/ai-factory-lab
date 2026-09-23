"""Orchestration layer: task lifecycle and agent dispatch.

This layer coordinates work but depends only on the ``AgentAdapter`` protocol
declared in the domain. It must never import a concrete agent engine directly.
"""

from factory.orchestration.lifecycle import (
    TERMINAL_STATES,
    TRANSITIONS,
    can_transition,
    next_states,
)
from factory.orchestration.machine import InvalidTransitionError, TaskStateMachine

__all__ = [
    "TERMINAL_STATES",
    "TRANSITIONS",
    "InvalidTransitionError",
    "TaskStateMachine",
    "can_transition",
    "next_states",
]
