"""Offline, disposable SQLite acceptance for the Factory-owned capability."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from factory.domain.enums import TaskKind, TaskStatus
from factory.domain.models import FactoryTask, TaskSource
from factory.domain.operational import (
    OperationalCapability,
    OperationalPolicy,
    canonical_operational_body,
    parse_database_readonly,
)
from factory.infrastructure.config import FactoryConfig
from factory.integrations.database_readonly import SqliteReadonlyInspector
from tests.test_operational import _runtime


def _fixture(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE items (value TEXT)")
        connection.executemany("INSERT INTO items VALUES (?)", [("secret-one",), ("secret-two",)])
        connection.execute("PRAGMA user_version=7")
        connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('rev-001')")


def _task(declaration: dict[str, object]) -> FactoryTask:
    return FactoryTask(
        title="Read SQLite",
        target_repository="example/control",
        source=TaskSource("github", "example/control", 82),
        kind=TaskKind.OPERATIONAL,
        labels=("factory-ready", "factory-operational"),
        body="untrusted instructions\n```factory-operational\n" + json.dumps(declaration) + "\n```",
    )


def test_disposable_end_to_end_never_dispatches_agent(tmp_path: Path) -> None:
    database = tmp_path / "target.db"
    _fixture(database)
    original_bytes = database.read_bytes()
    task = _task({"mode": "database_readonly", "target_id": "fixture"})
    runtime, tasks, runs, publisher, sink = _runtime(tmp_path, task, "missing-codex", github=True)
    runtime._database_inspector = SqliteReadonlyInspector({"fixture": str(database)})
    runtime._operational_policy = OperationalPolicy(
        enabled=frozenset({OperationalCapability.DATABASE_READONLY}),
        hosts=frozenset({"local"}),
        paths=frozenset({str(database)}),
        commands=frozenset({"inspect"}),
        targets=frozenset({"fixture"}),
    )

    result = runtime.run_once()

    assert result.outcome == "OPERATIONAL_DONE"
    stored = tasks.get(result.task_id or "")
    assert stored is not None and stored.status is TaskStatus.DONE
    assert "untrusted instructions" not in stored.body
    run = runs.list_runs(stored.task_id)[0]
    evidence = json.loads(run.summary or "")
    assert evidence["integrity"] == "ok"
    assert evidence["user_version"] == 7
    assert sorted(table["rows"] for table in evidence["tables"]) == [1, 2]
    assert evidence["migration_version_sha256"] == hashlib.sha256(b"rev-001").hexdigest()
    assert "items" not in run.summary
    assert "secret" not in run.summary
    assert str(database) not in run.summary
    assert publisher.calls == sink.create_calls == 0
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT count(*) FROM items").fetchone()[0] == 2
    assert database.read_bytes() == original_bytes


@pytest.mark.parametrize(
    "declaration",
    [
        {"mode": "database_readonly", "target_id": "db", "sql": "DELETE FROM items"},
        {"mode": "database_readonly", "target_id": "db", "path": "/tmp/db"},
        {"mode": "database_readonly", "target_id": "db", "command": "sqlite3"},
        {"mode": "database_readonly", "target_id": "../db"},
        {"mode": "database_readonly", "target_id": "db", "risk": "low"},
    ],
)
def test_issue_cannot_supply_sql_path_or_command(declaration: dict[str, object]) -> None:
    task = _task(declaration)
    with pytest.raises(ValueError):
        canonical_operational_body(task.body)
    with pytest.raises(ValueError):
        parse_database_readonly(task.body)


def test_unknown_target_and_unsafe_files_fail_closed(tmp_path: Path) -> None:
    database = tmp_path / "target.db"
    _fixture(database)
    inspector = SqliteReadonlyInspector({"fixture": str(database)})
    with pytest.raises(ValueError, match="registered"):
        inspector.inspect("other")
    link = tmp_path / "link.db"
    link.symlink_to(database)
    with pytest.raises(ValueError, match="unsafe"):
        SqliteReadonlyInspector({"link": str(link)}).inspect("link")
    with pytest.raises(ValueError, match="unsafe"):
        SqliteReadonlyInspector({"directory": str(tmp_path)}).inspect("directory")


def test_unregistered_target_blocks_before_execution(tmp_path: Path) -> None:
    task = _task({"mode": "database_readonly", "target_id": "unknown"})
    runtime, tasks, runs, _, _ = _runtime(tmp_path, task, "missing-codex")
    runtime._database_inspector = SqliteReadonlyInspector({"registered": str(tmp_path / "db")})
    runtime._operational_policy = OperationalPolicy(
        enabled=frozenset({OperationalCapability.DATABASE_READONLY}),
        hosts=frozenset({"local"}),
        paths=frozenset({str(tmp_path / "db")}),
        commands=frozenset({"inspect"}),
        targets=frozenset({"registered"}),
    )
    result = runtime.run_once()
    assert result.outcome == "OPERATIONAL_POLICY_BLOCKED"
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED  # type: ignore[union-attr]
    assert runs.list_runs(task.task_id) == []


def test_size_and_table_bounds(tmp_path: Path) -> None:
    database = tmp_path / "target.db"
    _fixture(database)
    with sqlite3.connect(database) as connection:
        for index in range(33):
            connection.execute(f"CREATE TABLE table_{index} (id INTEGER)")
    with pytest.raises(ValueError, match="table limit"):
        SqliteReadonlyInspector({"fixture": str(database)}).inspect("fixture")
    database.write_bytes(b"x" * (64 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="unsafe"):
        SqliteReadonlyInspector({"fixture": str(database)}).inspect("fixture")


def test_time_and_evidence_bounds(tmp_path: Path) -> None:
    database = tmp_path / "target.db"
    _fixture(database)
    ticks = iter((0.0,))

    def expired_clock() -> float:
        return next(ticks, 3.0)

    with pytest.raises(ValueError, match="timed out"):
        SqliteReadonlyInspector({"fixture": str(database)}, monotonic=expired_clock).inspect(
            "fixture"
        )
    with sqlite3.connect(database) as connection:
        for index in range(24):
            connection.execute(f"CREATE TABLE table_{index} (id INTEGER)")
    with pytest.raises(ValueError, match="evidence limit"):
        SqliteReadonlyInspector({"fixture": str(database)}).inspect("fixture")


def test_registry_is_operator_owned_and_redacted(tmp_path: Path) -> None:
    database = tmp_path / "target.db"
    config = FactoryConfig.from_env(
        {"FACTORY_DATABASE_READONLY_TARGETS": json.dumps({"fixture": str(database)})}
    )
    assert config.database_readonly_targets == (("fixture", str(database)),)
    assert str(database) not in json.dumps(config.redacted())
    for raw in ('{"fixture": "relative.db"}', '{"fixture": 3}', '{"..": "/tmp/x"}'):
        with pytest.raises(ValueError, match="FACTORY_DATABASE_READONLY_TARGETS"):
            FactoryConfig.from_env({"FACTORY_DATABASE_READONLY_TARGETS": raw})
