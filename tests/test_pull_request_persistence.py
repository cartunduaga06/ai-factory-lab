"""Persistence tests for pull requests.

Each test uses a temporary SQLite file and real repositories. No mocks.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, RunStatus, TaskStatus
from factory.domain.errors import DuplicatePullRequestError
from factory.domain.models import AgentRun, FactoryTask, PullRequest, TaskSource, Workspace
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "factory.db")


def _seed(db_path: str) -> tuple[SqliteTaskRepository, SqliteRunRepository, str, str]:
    tasks = SqliteTaskRepository(db_path)
    tasks.initialize()
    task = tasks.save(
        FactoryTask(
            title="Task",
            target_repository="example/target",
            source=TaskSource("github", "example/control", 1),
        )
    )
    runs = SqliteRunRepository(db_path)
    runs.initialize()
    workspace = Workspace(
        repository_slug="example/target", branch="factory/task-1/ws-1", path="/tmp/ws-1"
    )
    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.OTHER,
        run_id="run-1",
        status=RunStatus.SUCCEEDED,
        workspace=workspace,
    )
    runs.save_run(run)
    return tasks, runs, task.task_id, run.run_id


def _pr(
    run_id: str, task_id: str, *, number: int = 5, branch: str = "factory/task-1/ws-1"
) -> PullRequest:
    return PullRequest(
        repository_slug="example/target",
        head_branch=branch,
        base_branch="main",
        title="Add widget",
        body="body",
        number=number,
        url=f"https://example.invalid/{number}",
        task_id=task_id,
        run_id=run_id,
    )


def test_pr_persists_across_reopen(db_path: str) -> None:
    _, _, task_id, run_id = _seed(db_path)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    repo.save(_pr(run_id, task_id))

    reopened = SqlitePullRequestRepository(db_path)
    reopened.initialize()
    stored = reopened.get_for_run(run_id)

    assert stored is not None
    assert stored.number == 5
    assert stored.url == "https://example.invalid/5"
    assert stored.task_id == task_id
    assert stored.run_id == run_id
    assert stored.repository_slug == "example/target"
    assert stored.head_branch == "factory/task-1/ws-1"
    assert stored.base_branch == "main"
    assert isinstance(stored.opened_at, datetime)


def test_run_and_task_association_is_preserved(db_path: str) -> None:
    _, _, task_id, run_id = _seed(db_path)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    repo.save(_pr(run_id, task_id))

    stored = repo.get_for_run(run_id)
    assert stored is not None and stored.task_id == task_id and stored.run_id == run_id


def test_second_pr_for_same_run_is_refused(db_path: str) -> None:
    _, _, task_id, run_id = _seed(db_path)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    repo.save(_pr(run_id, task_id))

    with pytest.raises(DuplicatePullRequestError):
        repo.save(_pr(run_id, task_id, number=99))

    # The original row is untouched — no silent identity change.
    stored = repo.get_for_run(run_id)
    assert stored is not None and stored.number == 5


def test_second_pr_for_same_branch_is_refused(db_path: str) -> None:
    tasks, runs, task_id, run_id = _seed(db_path)
    # A second run on the same repository/head branch.
    second_run = AgentRun(
        task_id=task_id,
        adapter=AgentKind.OTHER,
        run_id="run-2",
        status=RunStatus.SUCCEEDED,
        workspace=Workspace(
            repository_slug="example/target", branch="factory/task-1/ws-1", path="/tmp/ws-2"
        ),
    )
    # The workspace uniqueness would reject this; use a distinct workspace but the
    # same branch to isolate the branch-level guard.
    second_run.workspace = Workspace(
        repository_slug="example/target", branch="factory/task-1/ws-1", path="/tmp/ws-9"
    )
    runs.save_run(second_run)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    repo.save(_pr(run_id, task_id, number=1))

    with pytest.raises(DuplicatePullRequestError):
        repo.save(_pr("run-2", task_id, number=2))


def test_retry_does_not_create_a_duplicate_row(db_path: str) -> None:
    _, _, task_id, run_id = _seed(db_path)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    repo.save(_pr(run_id, task_id, number=7))
    # Simulate the service's idempotent retry: on a duplicate, read the stored row.
    with pytest.raises(DuplicatePullRequestError):
        repo.save(_pr(run_id, task_id, number=7))

    rows = _raw_rows(db_path)
    assert len(rows) == 1


def test_find_by_branch(db_path: str) -> None:
    _, _, task_id, run_id = _seed(db_path)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    repo.save(_pr(run_id, task_id))

    found = repo.find_by_branch("example/target", "factory/task-1/ws-1")
    assert found is not None and found.number == 5
    assert repo.find_by_branch("example/other", "factory/task-1/ws-1") is None


def test_existing_database_gains_the_table_without_data_loss(db_path: str) -> None:
    # Initialize the older schema only (tasks, runs, workspaces), then add the
    # pull_requests table: existing rows must survive.
    tasks, _, task_id, run_id = _seed(db_path)
    before = tasks.get(task_id)
    assert before is not None

    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    repo.save(_pr(run_id, task_id))

    after = SqliteTaskRepository(db_path).get(task_id)
    assert after is not None and after.task_id == before.task_id
    assert len(_raw_rows(db_path)) == 1


def test_persisted_pr_can_repair_lifecycle_after_restart(db_path: str) -> None:
    """A reopened repository still reports the PR so publication can reconcile."""
    _, _, task_id, run_id = _seed(db_path)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    saved = repo.save(_pr(run_id, task_id))
    del saved

    reopened = SqlitePullRequestRepository(db_path)
    reopened.initialize()
    assert reopened.get_for_run(run_id) is not None


def test_duplicate_error_is_sanitized(db_path: str) -> None:
    _, _, task_id, run_id = _seed(db_path)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    repo.save(_pr(run_id, task_id))
    with pytest.raises(DuplicatePullRequestError) as caught:
        repo.save(_pr(run_id, task_id, number=3))
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def _raw_rows(db_path: str) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute("SELECT * FROM pull_requests").fetchall()
    finally:
        conn.close()


def test_status_and_merged_round_trip(db_path: str) -> None:
    _, _, task_id, run_id = _seed(db_path)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    pr = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task-1/ws-1",
        base_branch="main",
        title="t",
        number=1,
        run_id=run_id,
        task_id=task_id,
        opened_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
    )
    repo.save(pr)
    stored = repo.get_for_run(run_id)
    assert stored is not None
    assert stored.opened_at == datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    assert stored.merged is False


def test_task_status_is_not_mutated_by_pr_persistence(db_path: str) -> None:
    tasks, _, task_id, run_id = _seed(db_path)
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    repo.save(_pr(run_id, task_id))
    assert tasks.get(task_id).status is TaskStatus.DISCOVERED
