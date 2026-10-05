"""Compatibility exports for the authoritative domain task lifecycle graph."""

from __future__ import annotations

from factory.domain.task_lifecycle import (
    TERMINAL_STATES,
    TRANSITIONS,
    can_transition,
    next_states,
)

__all__ = ["TERMINAL_STATES", "TRANSITIONS", "can_transition", "next_states"]
