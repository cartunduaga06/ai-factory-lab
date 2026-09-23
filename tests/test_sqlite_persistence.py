"""Tests for SQLite-backed task persistence.

Every test uses a temporary database file so durability across repository
re-instantiation can be exercised for real. No test touches the network.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.domain.enums import TaskStatus
from factory.domain.errors import DuplicateTaskError
from factory.domain.models import FactoryTask, TaskSource
from factory.infrastructure.persistence import SqliteTaskRepository


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "factory.db")


def _source(number: int) -> TaskSource:
    return TaskSource("github", "cartunduaga06/ai-factory-lab", number)


def _task(
    number: int, *, title: str = "Task", status: TaskStatus = TaskStatus.DISCOVERED
) -> FactoryTask:
    return FactoryTask(
        title=title,
        target_repository="cartunduaga06/finanza-ia",
        source=_source(number),
        status=status,
        labels=("factory-ready", "backend"),
        body="body text",
    )


def _repo(db_path: str) -> SqliteTaskRepository:
    repository = SqliteTaskRepository(db_path)
    repository.initialize()
    return repository


# -- schema ----------------------------------------------------------------


def test_initialization_creates_usable_database(db_path: str) -> None:
    repository = _repo(db_path)
    assert Path(db_path).exists()
    assert repository.list() == []


def test_repeated_initialization_is_idempotent(db_path: str) -> None:
    repository = _repo(db_path)
    repository.save(_task(1))
    # Re-initializing must not drop data or fail.
    repository.initialize()
    repository.initialize()
    assert len(repository.list()) == 1


def test_missing_parent_directory_is_created(tmp_path: Path) -> None:
    nested = tmp_path / "nested" / "dir" / "factory.db"
    repository = SqliteTaskRepository(str(nested))
    repository.initialize()
    assert nested.exists()


# -- save / retrieve -------------------------------------------------------


def test_save_and_get_by_task_id(db_path: str) -> None:
    repository = _repo(db_path)
    task = _task(1)
    repository.save(task)

    fetched = repository.get(task.task_id)
    assert fetched is not None
    assert fetched.task_id == task.task_id
    assert fetched.title == "Task"


def test_get_unknown_task_returns_none(db_path: str) -> None:
    assert _repo(db_path).get("does-not-exist") is None


def test_find_by_structured_source_identity(db_path: str) -> None:
    repository = _repo(db_path)
    task = _task(42)
    repository.save(task)

    found = repository.find_by_source(_source(42))
    assert found is not None
    assert found.task_id == task.task_id
    assert repository.find_by_source(_source(43)) is None


def test_source_identity_lookup_is_provider_aware(db_path: str) -> None:
    repository = _repo(db_path)
    repository.save(_task(42))
    assert (
        repository.find_by_source(TaskSource("gitlab", "cartunduaga06/ai-factory-lab", 42)) is None
    )


def test_list_returns_all_tasks(db_path: str) -> None:
    repository = _repo(db_path)
    repository.save(_task(1))
    repository.save(_task(2))
    assert len(repository.list()) == 2


def test_list_filters_by_status(db_path: str) -> None:
    repository = _repo(db_path)
    repository.save(_task(1, status=TaskStatus.DISCOVERED))
    repository.save(_task(2, status=TaskStatus.READY))
    ready = repository.list(TaskStatus.READY)
    assert [task.source.issue_number for task in ready if task.source] == [2]


def test_update_persists_changes(db_path: str) -> None:
    repository = _repo(db_path)
    task = _task(1)
    repository.save(task)

    task.title = "Renamed"
    task.labels = ("factory-ready", "urgent")
    repository.update(task)

    fetched = repository.get(task.task_id)
    assert fetched is not None
    assert fetched.title == "Renamed"
    assert fetched.labels == ("factory-ready", "urgent")


def test_update_unknown_task_raises_key_error(db_path: str) -> None:
    repository = _repo(db_path)
    with pytest.raises(KeyError):
        repository.update(FactoryTask(title="x", target_repository="a/b"))


# -- uniqueness constraint -------------------------------------------------


def test_duplicate_source_identity_is_rejected_by_storage(db_path: str) -> None:
    repository = _repo(db_path)
    repository.save(_task(7))
    with pytest.raises(DuplicateTaskError) as excinfo:
        repository.save(_task(7, title="Different title"))
    assert excinfo.value.source == _source(7)
    # The original row is untouched.
    assert len(repository.list()) == 1


def test_source_less_tasks_do_not_collide(db_path: str) -> None:
    repository = _repo(db_path)
    repository.save(FactoryTask(title="a", target_repository="a/b"))
    repository.save(FactoryTask(title="b", target_repository="a/b"))
    assert len(repository.list()) == 2


# -- durability ------------------------------------------------------------


def test_data_survives_repository_re_instantiation(db_path: str) -> None:
    first = _repo(db_path)
    task = _task(1)
    first.save(task)

    second = SqliteTaskRepository(db_path)
    second.initialize()
    recovered = second.get(task.task_id)
    assert recovered is not None
    assert recovered.source == _source(1)
    assert recovered.title == "Task"


# -- serialization round-trips ---------------------------------------------


def test_labels_round_trip(db_path: str) -> None:
    repository = _repo(db_path)
    task = _task(1)
    task.labels = ("factory-ready", "backend", "urgent")
    repository.save(task)

    fetched = repository.get(task.task_id)
    assert fetched is not None
    assert fetched.labels == ("factory-ready", "backend", "urgent")


def test_empty_labels_round_trip(db_path: str) -> None:
    repository = _repo(db_path)
    task = _task(1)
    task.labels = ()
    repository.save(task)
    fetched = repository.get(task.task_id)
    assert fetched is not None
    assert fetched.labels == ()


def test_status_round_trips_every_value(db_path: str) -> None:
    repository = _repo(db_path)
    for index, status in enumerate(TaskStatus, start=1):
        task = _task(index, status=status)
        repository.save(task)
        fetched = repository.get(task.task_id)
        assert fetched is not None
        assert fetched.status is status


def test_timestamps_round_trip_with_timezone(db_path: str) -> None:
    repository = _repo(db_path)
    task = _task(1)
    repository.save(task)

    fetched = repository.get(task.task_id)
    assert fetched is not None
    assert fetched.created_at == task.created_at
    assert fetched.created_at.tzinfo is not None
    assert fetched.updated_at.tzinfo is not None
