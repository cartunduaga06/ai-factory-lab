"""Tests for SQLite-backed workspace and agent-run persistence.

Every test uses a temporary database file so durability across repository
re-instantiation is exercised for real. No test touches the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus
from factory.domain.errors import DuplicateRunError, PersistenceError
from factory.domain.models import AgentRun, FactoryTask, QualityGate, TaskSource, Workspace
from factory.infrastructure.persistence import SqliteRunRepository, SqliteTaskRepository

WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "factory.db")


def _runs(db_path: str) -> SqliteRunRepository:
    repository = SqliteRunRepository(db_path)
    repository.initialize()
    return repository


def _tasks(db_path: str) -> SqliteTaskRepository:
    repository = SqliteTaskRepository(db_path)
    repository.initialize()
    return repository


def _task(number: int = 1) -> FactoryTask:
    return FactoryTask(
        title="Task",
        target_repository="cartunduaga06/finanza-ia",
        source=TaskSource("github", "cartunduaga06/ai-factory-lab", number),
    )


def _workspace(task_id: str = "task-1", workspace_id: str = "ws-1") -> Workspace:
    return Workspace(
        workspace_id=workspace_id,
        repository_slug="cartunduaga06/finanza-ia",
        branch=f"factory/{task_id}",
        path=f"/tmp/.workspaces/{task_id}",
        created_at=WHEN,
    )


def _run(
    task_id: str = "task-1",
    *,
    status: RunStatus = RunStatus.PENDING,
    run_id: str = "run-1",
    workspace: Workspace | None = None,
) -> AgentRun:
    return AgentRun(
        task_id=task_id,
        adapter=AgentKind.OTHER,
        run_id=run_id,
        status=status,
        workspace=workspace if workspace is not None else _workspace(task_id),
        summary="did work",
        started_at=WHEN,
        finished_at=None,
    )


# -- workspaces ------------------------------------------------------------


def test_workspace_round_trips(db_path: str) -> None:
    repository = _runs(db_path)
    workspace = _workspace()
    repository.save_workspace(workspace)

    loaded = repository.get_workspace(workspace.workspace_id)
    assert loaded == workspace


def test_unknown_workspace_is_none(db_path: str) -> None:
    assert _runs(db_path).get_workspace("missing") is None


def test_duplicate_workspace_id_is_refused(db_path: str) -> None:
    repository = _runs(db_path)
    repository.save_workspace(_workspace())
    with pytest.raises(DuplicateRunError):
        repository.save_workspace(_workspace())


# -- agent runs ------------------------------------------------------------


def test_run_round_trips_with_workspace(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)

    run = _run(task.task_id)
    repository.save_run(run)

    loaded = repository.get_run(run.run_id)
    assert loaded is not None
    assert loaded.task_id == task.task_id
    assert loaded.adapter is AgentKind.OTHER
    assert loaded.status is RunStatus.PENDING
    assert loaded.summary == "did work"
    assert loaded.started_at == WHEN
    assert loaded.finished_at is None
    assert loaded.workspace == run.workspace


def test_run_without_workspace_round_trips(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)

    run = AgentRun(task_id=task.task_id, adapter=AgentKind.CODEX)
    repository.save_run(run)

    loaded = repository.get_run(run.run_id)
    assert loaded is not None
    assert loaded.workspace is None
    assert loaded.adapter is AgentKind.CODEX


def test_run_gates_round_trip(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)

    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.OTHER,
        gates=(
            QualityGate("lint", QualityGateStatus.PASSED),
            QualityGate("tests", QualityGateStatus.FAILED, detail="2 failing", required=True),
        ),
    )
    repository.save_run(run)

    loaded = repository.get_run(run.run_id)
    assert loaded is not None
    assert loaded.gates == run.gates


def test_unknown_run_is_none(db_path: str) -> None:
    assert _runs(db_path).get_run("missing") is None


def test_list_runs_filters_by_task(db_path: str) -> None:
    tasks = _tasks(db_path)
    first = tasks.save(_task(1))
    second = tasks.save(_task(2))
    repository = _runs(db_path)

    repository.save_run(
        AgentRun(task_id=first.task_id, adapter=AgentKind.OTHER, status=RunStatus.FAILED)
    )
    repository.save_run(
        AgentRun(task_id=second.task_id, adapter=AgentKind.OTHER, status=RunStatus.SUCCEEDED)
    )

    assert len(repository.list_runs()) == 2
    only_first = repository.list_runs(first.task_id)
    assert len(only_first) == 1
    assert only_first[0].task_id == first.task_id


def test_run_survives_repository_reinstantiation(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    run = _run(task.task_id)
    _runs(db_path).save_run(run)

    reopened = _runs(db_path)
    loaded = reopened.get_run(run.run_id)
    assert loaded is not None
    assert loaded.workspace is not None
    assert loaded.workspace.branch == f"factory/{task.task_id}"


# -- relationships and guards ---------------------------------------------


def test_run_relationship_to_task_and_workspace(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)

    assert repository.get_run(run.run_id).task_id == task.task_id
    assert repository.get_workspace(run.workspace.workspace_id) == run.workspace
    assert repository.find_active_run(task.task_id).run_id == run.run_id


def test_one_active_run_per_task_is_enforced(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    repository.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER))

    with pytest.raises(DuplicateRunError):
        repository.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER))


def test_terminal_run_allows_a_later_active_run(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    repository.save_run(
        AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=RunStatus.SUCCEEDED)
    )

    # A finished run does not block a retry.
    repository.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER))
    assert len(repository.list_runs(task.task_id)) == 2


def test_find_active_run_ignores_terminal_runs(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    repository.save_run(
        AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=RunStatus.FAILED)
    )
    assert repository.find_active_run(task.task_id) is None


def test_run_for_unknown_task_is_refused(db_path: str) -> None:
    # Foreign keys are enforced: a run cannot reference a task that is not stored.
    repository = _runs(db_path)
    with pytest.raises(Exception):  # noqa: B017 - sqlite integrity, engine-specific
        repository.save_run(AgentRun(task_id="missing", adapter=AgentKind.OTHER))


# -- schema ----------------------------------------------------------------


def test_repeated_initialization_is_idempotent(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    repository.save_run(_run(task.task_id))

    repository.initialize()
    repository.initialize()

    assert len(repository.list_runs()) == 1
    assert len(tasks.list()) == 1


def test_phase_2a_database_gains_run_schema_without_loss(db_path: str) -> None:
    # An existing Phase 2A database must remain valid once the new tables appear.
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    repository.initialize()

    assert tasks.get(task.task_id) == task


# -- durable updates (Phase 4) --------------------------------------------


def test_update_run_round_trips_status_summary_timestamps_and_gates(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)

    finished = datetime(2026, 2, 3, 4, 5, 6, tzinfo=UTC)
    run.status = RunStatus.SUCCEEDED
    run.summary = "implemented the change"
    run.started_at = WHEN
    run.finished_at = finished
    run.gates = (
        QualityGate("tests", QualityGateStatus.PASSED, detail="exit_code=0"),
        QualityGate("lint", QualityGateStatus.FAILED, detail="exit_code=1", required=False),
    )
    repository.update_run(run)

    reopened = _runs(db_path)
    loaded = reopened.get_run(run.run_id)
    assert loaded is not None
    assert loaded.status is RunStatus.SUCCEEDED
    assert loaded.summary == "implemented the change"
    assert loaded.started_at == WHEN
    assert loaded.finished_at == finished
    assert loaded.workspace == run.workspace
    assert loaded.gates == run.gates


def test_update_run_does_not_create_a_duplicate(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)

    repository.update_run(run)
    repository.update_run(run)

    assert len(repository.list_runs(task.task_id)) == 1


def test_update_run_preserves_task_identity(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)

    run.status = RunStatus.SUCCEEDED
    repository.update_run(run)

    loaded = repository.get_run(run.run_id)
    assert loaded is not None
    assert loaded.task_id == task.task_id
    assert loaded.run_id == run.run_id
    assert loaded.adapter is run.adapter


def test_update_unknown_run_fails_safely(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)

    with pytest.raises(KeyError):
        repository.update_run(run)
    # Refusing an unknown update must not have inserted it.
    assert repository.list_runs(task.task_id) == []


def test_update_run_refuses_to_re_point_at_another_task(db_path: str) -> None:
    tasks = _tasks(db_path)
    first = tasks.save(_task(1))
    second = tasks.save(_task(2))
    repository = _runs(db_path)
    run = _run(first.task_id)
    repository.save_run(run)

    run.task_id = second.task_id
    with pytest.raises(PersistenceError):
        repository.update_run(run)


def test_update_run_releases_the_active_run_slot_when_terminal(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)
    assert repository.find_active_run(task.task_id) is not None

    run.status = RunStatus.SUCCEEDED
    run.finished_at = WHEN
    repository.update_run(run)

    # Terminalising a run frees the one-active-run slot for a retry.
    assert repository.find_active_run(task.task_id) is None
    repository.save_run(AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER))
    assert len(repository.list_runs(task.task_id)) == 2


def test_update_run_workspace_is_not_duplicated(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)

    run.summary = "updated"
    repository.update_run(run)

    assert repository.get_workspace(run.workspace.workspace_id) == run.workspace  # type: ignore[union-attr]


# -- one workspace per run (Phase 4 isolation invariant) -------------------


def test_update_run_cannot_change_workspace(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)
    original = repository.get_run(run.run_id)
    assert original is not None and original.workspace is not None

    run.workspace = _workspace(task.task_id, workspace_id="ws-2")
    run.status = RunStatus.SUCCEEDED
    with pytest.raises(PersistenceError):
        repository.update_run(run)

    # The refusal must not have moved the run, and must not have stored ws-2.
    stored = repository.get_run(run.run_id)
    assert stored is not None
    assert stored.workspace == original.workspace
    assert repository.get_workspace("ws-2") is None


def test_update_run_cannot_clear_workspace(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)
    original = repository.get_run(run.run_id)
    assert original is not None and original.workspace is not None

    run.workspace = None
    with pytest.raises(PersistenceError):
        repository.update_run(run)

    stored = repository.get_run(run.run_id)
    assert stored is not None
    assert stored.workspace == original.workspace


def test_update_run_workspace_error_is_sanitized(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)

    run.workspace = Workspace(
        workspace_id="ws-2",
        repository_slug="cartunduaga06/finanza-ia",
        branch="factory/secret-branch/ws-2",
        path="/tmp/.workspaces/super-secret-path",
        created_at=WHEN,
    )
    with pytest.raises(PersistenceError) as excinfo:
        repository.update_run(run)

    rendered = f"{excinfo.value!s} {excinfo.value!r}"
    assert "super-secret-path" not in rendered
    assert "secret-branch" not in rendered
    assert "ws-2" not in rendered


def test_two_runs_cannot_share_a_workspace(db_path: str) -> None:
    tasks = _tasks(db_path)
    first = tasks.save(_task(1))
    second = tasks.save(_task(2))
    repository = _runs(db_path)

    shared = _workspace(first.task_id, workspace_id="ws-shared")
    repository.save_run(_run(first.task_id, run_id="run-1", workspace=shared))

    with pytest.raises(DuplicateRunError):
        repository.save_run(_run(second.task_id, run_id="run-2", workspace=shared))

    # The second run must not have been stored.
    assert repository.get_run("run-2") is None
    assert len(repository.list_runs(second.task_id)) == 0


def test_runs_without_workspace_do_not_collide(db_path: str) -> None:
    # The one-workspace-per-run index is partial: NULL workspaces are exempt.
    tasks = _tasks(db_path)
    first = tasks.save(_task(1))
    second = tasks.save(_task(2))
    repository = _runs(db_path)

    repository.save_run(
        AgentRun(task_id=first.task_id, adapter=AgentKind.OTHER, status=RunStatus.SUCCEEDED)
    )
    repository.save_run(
        AgentRun(task_id=second.task_id, adapter=AgentKind.OTHER, status=RunStatus.SUCCEEDED)
    )

    assert len(repository.list_runs()) == 2


def test_update_run_same_workspace_still_updates(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)

    run.status = RunStatus.SUCCEEDED
    run.gates = (QualityGate("tests", QualityGateStatus.PASSED, detail="exit_code=0"),)
    repository.update_run(run)

    loaded = repository.get_run(run.run_id)
    assert loaded is not None
    assert loaded.status is RunStatus.SUCCEEDED
    assert loaded.gates == run.gates
    assert loaded.workspace == run.workspace


def test_workspace_association_survives_reopen(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    run = _run(task.task_id)
    _runs(db_path).save_run(run)

    reopened = _runs(db_path)
    loaded = reopened.get_run(run.run_id)
    assert loaded is not None
    assert loaded.workspace is not None
    assert loaded.workspace.workspace_id == "ws-1"

    # A second update through the reopened repository keeps the same workspace.
    loaded.status = RunStatus.SUCCEEDED
    reopened.update_run(loaded)
    again = _runs(db_path).get_run(run.run_id)
    assert again is not None
    assert again.workspace is not None
    assert again.workspace.workspace_id == "ws-1"


# -- validated revision ----------------------------------------------------


def test_validated_revision_round_trips(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    run.validated_revision = "tree-abc123"
    repository.save_run(run)

    loaded = repository.get_run(run.run_id)
    assert loaded is not None
    assert loaded.validated_revision == "tree-abc123"


def test_validated_revision_is_none_by_default(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)

    loaded = repository.get_run(run.run_id)
    assert loaded is not None
    assert loaded.validated_revision is None


def test_validated_revision_survives_reopen(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    run = _run(task.task_id)
    run.validated_revision = "tree-reopen"
    _runs(db_path).save_run(run)

    reloaded = _runs(db_path).get_run(run.run_id)
    assert reloaded is not None
    assert reloaded.validated_revision == "tree-reopen"


def test_update_run_persists_validated_revision(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    repository.save_run(run)
    assert repository.get_run(run.run_id).validated_revision is None  # type: ignore[union-attr]

    run.status = RunStatus.SUCCEEDED
    run.gates = (QualityGate("tests", QualityGateStatus.PASSED),)
    run.validated_revision = "tree-after-validation"
    repository.update_run(run)

    loaded = _runs(db_path).get_run(run.run_id)
    assert loaded is not None
    assert loaded.validated_revision == "tree-after-validation"
    # Unrelated identity is unchanged.
    assert loaded.task_id == task.task_id
    assert loaded.workspace is not None
    assert loaded.workspace.workspace_id == "ws-1"


def test_validated_revision_can_be_cleared(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    run.validated_revision = "tree-then-cleared"
    repository.save_run(run)

    run.validated_revision = None
    repository.update_run(run)

    loaded = _runs(db_path).get_run(run.run_id)
    assert loaded is not None
    assert loaded.validated_revision is None


def test_old_database_migrates_without_data_loss(tmp_path: Path) -> None:
    # A Phase 4/5 database created before revision binding: build the runs table
    # without ``validated_revision``, insert a row, then initialize.
    import sqlite3

    db_path = str(tmp_path / "legacy.db")
    tasks = SqliteTaskRepository(db_path)
    tasks.initialize()
    task = tasks.save(_task())

    conn = sqlite3.connect(db_path)
    # Recreate the legacy shape: drop and recreate agent_runs without the column.
    conn.execute("PRAGMA foreign_keys = OFF")
    conn.execute("DROP TABLE IF EXISTS agent_runs")
    conn.execute(
        """
        CREATE TABLE agent_runs (
            run_id        TEXT PRIMARY KEY,
            task_id       TEXT NOT NULL,
            adapter       TEXT NOT NULL,
            status        TEXT NOT NULL,
            workspace_id  TEXT,
            summary       TEXT,
            started_at    TEXT,
            finished_at   TEXT,
            gates         TEXT NOT NULL DEFAULT '[]',
            created_at    TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        INSERT INTO agent_runs (
            run_id, task_id, adapter, status, summary, gates, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("legacy-run", task.task_id, "OTHER", "FAILED", "old", "[]", "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()

    # Initialize the current repository: it must add the column idempotently.
    repository = _runs(db_path)

    loaded = repository.get_run("legacy-run")
    assert loaded is not None
    assert loaded.status is RunStatus.FAILED
    assert loaded.summary == "old"
    # An old row loads with no validated revision.
    assert loaded.validated_revision is None

    # A new run with a revision can be stored alongside the migrated row.
    new_run = AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, run_id="new-run")
    new_run.validated_revision = "tree-new"
    repository.save_run(new_run)
    assert repository.get_run("new-run").validated_revision == "tree-new"  # type: ignore[union-attr]
    # The legacy row is still intact.
    assert repository.get_run("legacy-run") is not None


def test_migration_is_idempotent(db_path: str) -> None:
    # Repeated initialization on a current database is a no-op, not an error.
    tasks = _tasks(db_path)
    task = tasks.save(_task())
    repository = _runs(db_path)
    run = _run(task.task_id)
    run.validated_revision = "tree-keep"
    repository.save_run(run)

    for _ in range(3):
        repository.initialize()

    loaded = repository.get_run(run.run_id)
    assert loaded is not None
    assert loaded.validated_revision == "tree-keep"
