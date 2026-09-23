"""Task lifecycle: allowed status transitions.

This is intentionally a small, declarative transition table rather than a
workflow engine. It is the single source of truth for which state changes are
legal, and it is enforced by :class:`factory.orchestration.machine.TaskStateMachine`.

See ``docs/architecture.md`` for the diagram.
"""

from __future__ import annotations

from types import MappingProxyType

from factory.domain.enums import TaskStatus

# Terminal states accept no further transitions.
TERMINAL_STATES: frozenset[TaskStatus] = frozenset(
    {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
)

TRANSITIONS: MappingProxyType[TaskStatus, frozenset[TaskStatus]] = MappingProxyType(
    {
        TaskStatus.DISCOVERED: frozenset(
            {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.FAILED}
        ),
        TaskStatus.READY: frozenset({TaskStatus.CLAIMED, TaskStatus.BLOCKED, TaskStatus.CANCELLED}),
        TaskStatus.CLAIMED: frozenset(
            {TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED}
        ),
        TaskStatus.RUNNING: frozenset(
            {TaskStatus.VALIDATING, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.VALIDATING: frozenset(
            {TaskStatus.PR_OPEN, TaskStatus.READY, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.PR_OPEN: frozenset(
            {TaskStatus.WAITING_HUMAN, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.WAITING_HUMAN: frozenset(
            {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        # BLOCKED is the only non-terminal recovery state: it must be able to
        # re-enter the pipeline once the blocker is removed.
        TaskStatus.BLOCKED: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
        TaskStatus.DONE: frozenset(),
        TaskStatus.FAILED: frozenset(),
        TaskStatus.CANCELLED: frozenset(),
    }
)


def next_states(status: TaskStatus) -> frozenset[TaskStatus]:
    """Return the states reachable from ``status`` in one step."""
    return TRANSITIONS[status]


def can_transition(source: TaskStatus, target: TaskStatus) -> bool:
    """Return whether ``source -> target`` is a legal lifecycle transition."""
    return target in TRANSITIONS[source]
