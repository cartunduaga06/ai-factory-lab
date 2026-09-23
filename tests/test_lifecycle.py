"""Tests for the task lifecycle state machine."""

from __future__ import annotations

import pytest

from factory.domain.enums import TaskStatus
from factory.domain.models import FactoryTask
from factory.orchestration.lifecycle import TERMINAL_STATES, TRANSITIONS, can_transition
from factory.orchestration.machine import InvalidTransitionError, TaskStateMachine

HAPPY_PATH = [
    TaskStatus.DISCOVERED,
    TaskStatus.READY,
    TaskStatus.CLAIMED,
    TaskStatus.RUNNING,
    TaskStatus.VALIDATING,
    TaskStatus.PR_OPEN,
    TaskStatus.WAITING_HUMAN,
    TaskStatus.DONE,
]


def _task() -> FactoryTask:
    return FactoryTask(title="Implement feature", repository_slug="cartunduaga06/finanza-ia")


def test_happy_path_is_fully_traversable() -> None:
    machine = TaskStateMachine()
    task = _task()
    for target in HAPPY_PATH[1:]:
        machine.apply(task, target)
    assert task.status is TaskStatus.DONE
    assert machine.is_terminal(task.status)


def test_failure_paths_are_reachable() -> None:
    machine = TaskStateMachine()

    cancelled = _task()
    machine.apply(cancelled, TaskStatus.CANCELLED)
    assert cancelled.status is TaskStatus.CANCELLED

    blocked = _task()
    machine.apply(blocked, TaskStatus.READY)
    machine.apply(blocked, TaskStatus.BLOCKED)
    assert blocked.status is TaskStatus.BLOCKED

    failed = _task()
    for step in (TaskStatus.READY, TaskStatus.CLAIMED, TaskStatus.RUNNING, TaskStatus.FAILED):
        machine.apply(failed, step)
    assert failed.status is TaskStatus.FAILED


def test_blocked_can_recover_to_ready() -> None:
    machine = TaskStateMachine()
    task = _task()
    machine.apply(task, TaskStatus.READY)
    machine.apply(task, TaskStatus.BLOCKED)
    machine.apply(task, TaskStatus.READY)
    assert task.status is TaskStatus.READY


def test_illegal_transition_is_rejected() -> None:
    machine = TaskStateMachine()
    task = _task()
    with pytest.raises(InvalidTransitionError) as excinfo:
        machine.apply(task, TaskStatus.DONE)
    assert excinfo.value.source is TaskStatus.DISCOVERED
    assert excinfo.value.target is TaskStatus.DONE
    assert task.status is TaskStatus.DISCOVERED


def test_transition_table_covers_all_states() -> None:
    assert set(TRANSITIONS) == set(TaskStatus)


def test_terminal_states_are_sinks() -> None:
    assert {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED} == TERMINAL_STATES
    for state in TERMINAL_STATES:
        assert TRANSITIONS[state] == frozenset()


def test_can_transition_matches_table() -> None:
    assert can_transition(TaskStatus.DISCOVERED, TaskStatus.READY) is True
    assert can_transition(TaskStatus.DONE, TaskStatus.READY) is False
