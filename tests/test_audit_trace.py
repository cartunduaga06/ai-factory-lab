"""Trace records follow committed lifecycle facts across process restarts."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus, TaskStatus
from factory.domain.models import AgentRun, FactoryTask, PullRequest, QualityGate, TaskSource
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.pr_sqlite import SqlitePullRequestRepository
from factory.infrastructure.persistence.run_sqlite import SqliteRunRepository
from factory.infrastructure.persistence.schema import (
    AUDIT_EVENTS_TABLE,
    CREATE_AGENT_RUNS,
    CREATE_PULL_REQUESTS,
    CREATE_TASKS,
    CREATE_TRANSITIONS,
)
from factory.infrastructure.persistence.sqlite import SqliteTaskRepository


def test_trace_is_ordered_durable_idempotent_and_sanitized(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(path)
    runs = SqliteRunRepository(path)
    prs = SqlitePullRequestRepository(path)
    for repo in (tasks, runs, prs):
        repo.initialize()
    task = FactoryTask(
        title="ghp_secret is never evidence",
        body="token=private",
        target_repository="example/project",
        source=TaskSource("github", "example/project", 61),
    )
    tasks.save(task)
    tasks.apply_transition(task.task_id, TaskStatus.DISCOVERED, TaskStatus.READY)
    tasks.apply_transition(task.task_id, TaskStatus.READY, TaskStatus.CLAIMED)
    run = AgentRun(task_id=task.task_id, adapter=AgentKind.CODEX, run_id="ghp_secret_run")
    runs.save_run(run)
    run.status = RunStatus.SUCCEEDED
    run.gates = (QualityGate("pytest", QualityGateStatus.FAILED, "token=private"),)
    runs.update_run(run)
    runs.update_run(run)  # watcher retry
    tasks.apply_transition(task.task_id, TaskStatus.CLAIMED, TaskStatus.RUNNING)
    tasks.apply_transition(task.task_id, TaskStatus.RUNNING, TaskStatus.VALIDATING)

    restarted = SqliteAuditEventStore(path)
    events = restarted.for_issue("github", "example/project", 61)
    assert [event.sequence for event in events] == list(range(1, len(events) + 1))
    assert {event.correlation_id for event in events} == {task.task_id}
    assert [event.name for event in events].count("RunFinished") == 1
    assert [event.name for event in events].count("ValidationFailed") == 1
    assert all(event.aggregate_version >= 1 and event.causation_id for event in events)
    assert "ghp_secret" not in repr(events)
    assert "token=private" not in repr(events)


def test_initially_ready_task_has_ready_milestone(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(path)
    tasks.initialize()
    task = FactoryTask(title="ready", target_repository="example/project", status=TaskStatus.READY)
    tasks.save(task)
    assert [event.name for event in SqliteAuditEventStore(path).for_task(task.task_id)] == [
        "IssueMaterialized",
        "WorkItemReady",
    ]


def test_pr_rework_and_completion_share_trace(tmp_path: Path) -> None:
    path = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(path)
    runs = SqliteRunRepository(path)
    prs = SqlitePullRequestRepository(path)
    tasks.initialize()
    task = FactoryTask(title="test", target_repository="example/project")
    tasks.save(task)
    for source, target in (
        (TaskStatus.DISCOVERED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
        (TaskStatus.CLAIMED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.VALIDATING),
    ):
        tasks.apply_transition(task.task_id, source, target)
    run = AgentRun(task_id=task.task_id, adapter=AgentKind.CODEX)
    runs.save_run(run)
    prs.save(
        PullRequest(
            repository_slug="example/project",
            head_branch="factory/test",
            base_branch="main",
            title="test",
            number=1,
            task_id=task.task_id,
            run_id=run.run_id,
        )
    )
    tasks.apply_transition(task.task_id, TaskStatus.VALIDATING, TaskStatus.PR_OPEN)
    tasks.apply_transition(task.task_id, TaskStatus.PR_OPEN, TaskStatus.WAITING_HUMAN)
    tasks.request_rework(task.task_id, run.run_id, "change tests")
    for source, target in (
        (TaskStatus.CHANGES_REQUESTED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
        (TaskStatus.CLAIMED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.VALIDATING),
        (TaskStatus.VALIDATING, TaskStatus.PR_OPEN),
    ):
        tasks.apply_transition(task.task_id, source, target)
    tasks.apply_transition(task.task_id, TaskStatus.PR_OPEN, TaskStatus.WAITING_HUMAN)
    tasks.apply_transition(task.task_id, TaskStatus.WAITING_HUMAN, TaskStatus.DONE)
    events = SqliteAuditEventStore(path).for_task(task.task_id)
    names = [event.name for event in events]
    assert names.count("PRCreated") == 1
    assert names.count("PRUpdated") == 1
    assert "HumanApprovalRequired" in names
    assert "ChangesRequested" in names
    assert "TaskCompleted" in names
    assert any(event.pull_request_id for event in events)
    assert all(event.task_id == task.task_id for event in events)


def test_existing_database_is_migrated_without_rewriting_tasks(tmp_path: Path) -> None:
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as conn:
        conn.execute(CREATE_TASKS)
        conn.execute(CREATE_TRANSITIONS)
        conn.execute(CREATE_AGENT_RUNS)
        conn.execute(CREATE_PULL_REQUESTS)
        conn.execute(
            "INSERT INTO tasks (task_id, title, target_repository, status, created_at, updated_at) "
            "VALUES ('old', 'old', 'example/project', 'READY', '2026-01-01', '2026-01-01')"
        )
        conn.execute(
            "INSERT INTO transitions (transition_id, task_id, from_status, to_status, "
            "occurred_at) VALUES ('edge', 'old', 'DISCOVERED', 'READY', '2026-01-02')"
        )
        conn.execute(
            "INSERT INTO agent_runs (run_id, task_id, adapter, status, gates, "
            "created_at, finished_at) VALUES "
            "('run-old', 'old', 'CODEX', 'SUCCEEDED', '[]', '2026-01-03', '2026-01-04')"
        )
        conn.execute(
            "INSERT INTO pull_requests (pull_request_id, run_id, repository_slug, head_branch, "
            "base_branch, title, opened_at) VALUES "
            "('pr-old', 'run-old', 'example/project', 'factory/old', 'main', 'old', '2026-01-05')"
        )
    repo = SqliteTaskRepository(path)
    repo.initialize()
    repo.initialize()
    assert repo.get("old") is not None
    assert [event.name for event in SqliteAuditEventStore(path).for_task("old")] == [
        "IssueMaterialized",
        "WorkItemReady",
        "RunStarted",
        "RunFinished",
        "ValidationPassed",
        "PRCreated",
    ]
    with sqlite3.connect(path) as conn:
        assert conn.execute(f"SELECT count(*) FROM {AUDIT_EVENTS_TABLE}").fetchone() is not None
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"DELETE FROM {AUDIT_EVENTS_TABLE}")
