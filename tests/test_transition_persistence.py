"""Tests for lifecycle transitions with durable history.

These cover the seam between the pure state machine and persistence: valid
transitions update status and write history; invalid transitions touch nothing.
Atomicity is verified by forcing the history insert to fail and confirming the
status update rolls back with it.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from factory.domain.enums import TaskStatus
from factory.domain.models import FactoryTask, TaskSource
from factory.infrastructure.persistence import SqliteTaskRepository
from factory.orchestration.machine import InvalidTransitionError
from factory.orchestration.transitions import TaskLifecycleService

TASKS = "tasks"
TRANSITIONS = "transitions"


@pytest.fixture
def repo(tmp_path: Path) -> SqliteTaskRepository:
    repository = SqliteTaskRepository(str(tmp_path / "factory.db"))
    repository.initialize()
    return repository


def _saved(repo: SqliteTaskRepository, number: int = 1) -> FactoryTask:
    task = FactoryTask(
        title=f"Issue {number}",
        target_repository="cartunduaga06/finanza-ia",
        source=TaskSource("github", "cartunduaga06/ai-factory-lab", number),
    )
    repo.save(task)
    return task


def _service(repo: SqliteTaskRepository) -> TaskLifecycleService:
    return TaskLifecycleService(repo)


# -- valid transitions -----------------------------------------------------


def test_valid_transition_updates_status(repo: SqliteTaskRepository) -> None:
    task = _saved(repo)
    updated = _service(repo).transition(task.task_id, TaskStatus.READY)
    assert updated.status is TaskStatus.READY

    stored = repo.get(task.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.READY


def test_valid_transition_writes_history(repo: SqliteTaskRepository) -> None:
    task = _saved(repo)
    _service(repo).transition(task.task_id, TaskStatus.READY)

    history = repo.history(task.task_id)
    assert len(history) == 1
    assert history[0].from_status is TaskStatus.DISCOVERED
    assert history[0].to_status is TaskStatus.READY
    assert history[0].task_id == task.task_id


def test_history_preserves_full_chain(repo: SqliteTaskRepository) -> None:
    task = _saved(repo)
    service = _service(repo)
    for target in (TaskStatus.READY, TaskStatus.CLAIMED, TaskStatus.RUNNING):
        service.transition(task.task_id, target)

    history = service.history(task.task_id)
    assert [(entry.from_status, entry.to_status) for entry in history] == [
        (TaskStatus.DISCOVERED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
        (TaskStatus.CLAIMED, TaskStatus.RUNNING),
    ]


def test_history_is_ordered_oldest_first(repo: SqliteTaskRepository) -> None:
    task = _saved(repo)
    service = _service(repo)
    service.transition(task.task_id, TaskStatus.READY)
    service.transition(task.task_id, TaskStatus.CLAIMED)

    history = service.history(task.task_id)
    assert history[0].occurred_at <= history[1].occurred_at


def test_transition_updates_timestamp(repo: SqliteTaskRepository) -> None:
    task = _saved(repo)
    before = repo.get(task.task_id)
    assert before is not None

    updated = _service(repo).transition(task.task_id, TaskStatus.READY)
    assert updated.updated_at >= before.updated_at


# -- invalid transitions ---------------------------------------------------


def test_invalid_transition_raises(repo: SqliteTaskRepository) -> None:
    task = _saved(repo)
    with pytest.raises(InvalidTransitionError):
        _service(repo).transition(task.task_id, TaskStatus.DONE)


def test_invalid_transition_leaves_status_unchanged(repo: SqliteTaskRepository) -> None:
    task = _saved(repo)
    with pytest.raises(InvalidTransitionError):
        _service(repo).transition(task.task_id, TaskStatus.DONE)

    stored = repo.get(task.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.DISCOVERED


def test_invalid_transition_writes_no_history(repo: SqliteTaskRepository) -> None:
    task = _saved(repo)
    with pytest.raises(InvalidTransitionError):
        _service(repo).transition(task.task_id, TaskStatus.DONE)
    assert repo.history(task.task_id) == []


def test_unknown_task_raises_key_error(repo: SqliteTaskRepository) -> None:
    with pytest.raises(KeyError):
        _service(repo).transition("missing", TaskStatus.READY)


# -- atomicity -------------------------------------------------------------


def test_failed_history_insert_rolls_back_status_update(repo: SqliteTaskRepository) -> None:
    """Status and history must commit together or not at all.

    A trigger is installed that aborts every insert into ``transitions``. The
    status update is issued first inside the same transaction; when the history
    insert aborts, the whole transaction must roll back so the stored status is
    unchanged and no history remains.
    """
    task = _saved(repo)

    with sqlite3.connect(repo.path) as conn:
        conn.execute(
            """
            CREATE TRIGGER fail_transition_insert
            BEFORE INSERT ON transitions
            BEGIN
                SELECT RAISE(ABORT, 'simulated history insert failure');
            END;
            """
        )

    with pytest.raises(sqlite3.IntegrityError):
        _service(repo).transition(task.task_id, TaskStatus.READY)

    stored = repo.get(task.task_id)
    assert stored is not None
    assert stored.status is TaskStatus.DISCOVERED
    assert repo.history(task.task_id) == []


def test_lost_update_is_refused(repo: SqliteTaskRepository) -> None:
    """A compare-and-swap mismatch must not silently apply a stale transition."""
    from factory.domain.errors import TaskStateChangedError

    task = _saved(repo)
    _service(repo).transition(task.task_id, TaskStatus.READY)

    # Simulate a caller that validated against DISCOVERED after storage moved on.
    with pytest.raises(TaskStateChangedError):
        repo.apply_transition(task.task_id, TaskStatus.DISCOVERED, TaskStatus.CLAIMED)


def test_apply_transition_on_unknown_task_raises(repo: SqliteTaskRepository) -> None:
    with pytest.raises(KeyError):
        repo.apply_transition("missing", TaskStatus.DISCOVERED, TaskStatus.READY)
