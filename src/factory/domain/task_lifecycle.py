"""Authoritative task lifecycle graph shared by orchestration and persistence."""

from __future__ import annotations

from types import MappingProxyType

from factory.domain.enums import TaskStatus

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
            {
                TaskStatus.PR_OPEN,
                TaskStatus.READY,
                TaskStatus.BLOCKED,
                TaskStatus.DONE,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }
        ),
        TaskStatus.PR_OPEN: frozenset(
            {TaskStatus.WAITING_HUMAN, TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.WAITING_HUMAN: frozenset(
            {TaskStatus.CHANGES_REQUESTED, TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
        ),
        TaskStatus.CHANGES_REQUESTED: frozenset({TaskStatus.READY, TaskStatus.CANCELLED}),
        TaskStatus.BLOCKED: frozenset(
            {TaskStatus.READY, TaskStatus.VALIDATING, TaskStatus.CANCELLED}
        ),
        TaskStatus.DONE: frozenset(),
        TaskStatus.FAILED: frozenset(),
        TaskStatus.CANCELLED: frozenset(),
    }
)


def next_states(status: TaskStatus) -> frozenset[TaskStatus]:
    """Return the states reachable from ``status`` in one step."""
    return TRANSITIONS[status]


PERSISTENCE_ONLY_TRANSITIONS: MappingProxyType[
    TaskStatus, frozenset[TaskStatus]
] = MappingProxyType(
    {
        # Recovery/reconciliation adapters emit these explicit durable paths;
        # normal orchestration still uses the stricter state-machine graph.
        TaskStatus.READY: frozenset({TaskStatus.WAITING_HUMAN}),
        TaskStatus.RUNNING: frozenset({TaskStatus.WAITING_HUMAN}),
        TaskStatus.FAILED: frozenset({TaskStatus.READY}),
    }
)


def can_transition(source: TaskStatus, target: TaskStatus) -> bool:
    """Return whether source -> target is a normal lifecycle transition."""
    return target in TRANSITIONS[source]


def can_persist_transition(source: TaskStatus, target: TaskStatus) -> bool:
    """Return whether a durable adapter may apply this explicit transition."""
    return can_transition(source, target) or target in PERSISTENCE_ONLY_TRANSITIONS.get(
        source, frozenset()
    )
