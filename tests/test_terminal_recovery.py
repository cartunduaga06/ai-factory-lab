"""Regression tests: terminal Codex timeout must not discard partial work."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread

import pytest

from factory.domain.enums import AgentKind, RunStatus, TaskStatus
from factory.domain.errors import TaskStateChangedError
from factory.domain.models import AgentRun, FactoryTask, PullRequest, TaskSource, new_workspace
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.orchestration.machine import TaskStateMachine
from factory.orchestration.recovery import RecoveryPolicy
from factory.orchestration.retry import RetryService
from factory.orchestration.terminal_recovery import (
    TerminalRecoveryRefused,
    TerminalRecoveryService,
)
from tests.fake_publish import FakePullRequestSink
from tests.fake_workspace import FakeWorkspaceProvisioner


@pytest.fixture
def timeout_case(tmp_path: Path):
    root = tmp_path / "workspaces"
    root.mkdir()
    db = str(tmp_path / "factory.db")
    tasks, runs, prs = (
        SqliteTaskRepository(db),
        SqliteRunRepository(db),
        SqlitePullRequestRepository(db),
    )
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = tasks.save(
        FactoryTask(
            "Interrupted Codex",
            "example/target",
            status=TaskStatus.FAILED,
            source=TaskSource("github", "example/control", 67),
        )
    )
    workspace = new_workspace(task, str(root))
    path = Path(workspace.path)
    path.mkdir()
    (path / "partial-work.py").write_text("preserve = True\n")
    run = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.CODEX,
            status=RunStatus.FAILED,
            workspace=workspace,
            started_at=datetime.now(UTC) - timedelta(minutes=30),
            finished_at=datetime.now(UTC),
        )
    )
    state = root / ".factory-codex-runs"
    state.mkdir()
    (state / (run.run_id + ".result")).write_text(
        json.dumps(
            {
                "status": "FAILED",
                "exit_code": -9,
                "stdout_bytes": 0,
                "stderr_bytes": 1048576,
                "timed_out": True,
            }
        )
    )
    sink = FakePullRequestSink()
    availability = [True]
    service = TerminalRecoveryService(
        tasks,
        runs,
        prs,
        sink,
        FakeWorkspaceProvisioner(),
        workspace_root=str(root),
        base_branch="main",
        source_is_eligible=lambda _: availability[0],
    )
    return service, task, run, root, tasks, runs, prs, sink, availability


def test_terminal_failed_remains_terminal_without_explicit_recovery(timeout_case) -> None:
    service, task, run, _, tasks, runs, _, _, _ = timeout_case
    assert not TaskStateMachine().can_apply(task, TaskStatus.BLOCKED)
    with pytest.raises(TerminalRecoveryRefused, match="acknowledgement"):
        service.authorize(task.task_id, run.run_id, acknowledge_timeout=False)
    assert tasks.get(task.task_id).status is TaskStatus.FAILED
    assert len(runs.list_runs(task.task_id)) == 1
    assert tasks.history(task.task_id) == []


def test_operator_recovery_preserves_workspace_history_and_audit(timeout_case) -> None:
    service, task, run, root, tasks, runs, _, _, _ = timeout_case
    result = service.authorize(task.task_id, run.run_id, acknowledge_timeout=True)
    assert result.status is TaskStatus.BLOCKED
    assert result.blocked_reason == f"terminal-timeout-recovery:{run.run_id}"
    assert (Path(run.workspace.path) / "partial-work.py").read_text() == "preserve = True\n"
    assert [r.run_id for r in runs.list_runs(task.task_id)] == [run.run_id]
    assert [(t.from_status, t.to_status) for t in tasks.history(task.task_id)] == [
        (TaskStatus.FAILED, TaskStatus.BLOCKED)
    ]
    assert "TaskBLOCKED" in [
        e.name for e in SqliteAuditEventStore(tasks.path).for_task(task.task_id)
    ]
    assert (
        RetryService(tasks, runs, RecoveryPolicy(base_backoff_seconds=0)).retry(task.task_id).status
        is TaskStatus.READY
    )
    assert tasks.get(task.task_id).blocked_reason == f"terminal-timeout-recovery:{run.run_id}"
    assert root.exists()


@pytest.mark.parametrize(
    "invalid",
    [
        "wrong_run",
        "bad_evidence",
        "no_deadline",
        "legacy",
        "ineligible",
        "provider_pr",
        "local_pr",
        "busy",
    ],
)
def test_recovery_fails_closed_without_mutating_existing_state(timeout_case, invalid: str) -> None:
    service, task, run, root, tasks, runs, prs, sink, availability = timeout_case
    if invalid == "bad_evidence":
        (root / ".factory-codex-runs" / (run.run_id + ".result")).write_text(
            json.dumps({"status": "FAILED", "exit_code": 1})
        )
    elif invalid == "legacy":
        (root / ".factory-codex-runs" / (run.run_id + ".result")).write_text(
            json.dumps({"status": "FAILED", "exit_code": -9})
        )
    elif invalid == "no_deadline":
        (root / ".factory-codex-runs" / (run.run_id + ".result")).write_text(
            json.dumps({"status": "FAILED", "exit_code": -9, "timed_out": False})
        )
    elif invalid == "ineligible":
        availability[0] = False
    elif invalid in {"provider_pr", "local_pr"}:
        pr = PullRequest(
            repository_slug=task.target_repository,
            head_branch=run.workspace.branch,
            base_branch="main",
            title="Already published",
            number=68,
            url="https://example.invalid/68",
            task_id=task.task_id,
            run_id=run.run_id,
        )
        if invalid == "provider_pr":
            sink.seed(pr)
        else:
            prs.save(pr)
    elif invalid == "busy":
        other = tasks.save(FactoryTask("Other", "example/target"))
        runs.save_run(AgentRun(other.task_id, AgentKind.CODEX, RunStatus.RUNNING))
    with pytest.raises((TerminalRecoveryRefused, ValueError)):
        service.authorize(
            task.task_id,
            "00000000-0000-0000-0000-000000000001" if invalid == "wrong_run" else run.run_id,
            acknowledge_timeout=True,
        )
    assert tasks.get(task.task_id).status is TaskStatus.FAILED
    assert tasks.history(task.task_id) == []


def test_concurrent_terminal_authorizations_only_one_wins(timeout_case) -> None:
    service, task, run, _, tasks, runs, _, _, _ = timeout_case
    barrier = Barrier(2)
    outcomes: list[str] = []

    def worker() -> None:
        barrier.wait()
        try:
            service.authorize(task.task_id, run.run_id, acknowledge_timeout=True)
            outcomes.append("approved")
        except (TerminalRecoveryRefused, TaskStateChangedError, ValueError):
            outcomes.append("rejected")

    a, b = Thread(target=worker), Thread(target=worker)
    a.start()
    b.start()
    a.join()
    b.join()
    assert sorted(outcomes) == ["approved", "rejected"]
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED
    assert len(tasks.history(task.task_id)) == 1
    assert [r.run_id for r in runs.list_runs(task.task_id)] == [run.run_id]


def test_authorized_retry_reuses_old_workspace_instead_of_creating_second(tmp_path: Path) -> None:
    from tests.test_runtime import runtime_parts as make_runtime

    # Reuse the real offline factory runtime fixture with CODEX as its engine.
    # Its fake Git provider and gate runners do not access external services.
    runtime, tasks, runs, adapter, publisher, sink, _ = make_runtime.__wrapped__(tmp_path)
    adapter._kind = AgentKind.CODEX
    task = tasks.save(
        FactoryTask(
            "Issue 8 acceptance",
            "example/target",
            source=TaskSource("github", "example/control", 8),
            status=TaskStatus.READY,
        )
    )
    workspace = new_workspace(task, str(tmp_path / "host-workspaces"))
    path = Path(workspace.path)
    path.mkdir(parents=True)
    (path / "partial.py").write_text("original_change=True\n")
    previous = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.CODEX,
            status=RunStatus.FAILED,
            workspace=workspace,
            finished_at=datetime.now(UTC),
        )
    )
    task.blocked_reason = f"terminal-timeout-recovery:{previous.run_id}"
    tasks.update(task)

    result = runtime.run_once()
    assert result.task_status is TaskStatus.WAITING_HUMAN
    assert len(runs.list_runs(task.task_id)) == 2
    assert adapter.dispatched == [(task.task_id, workspace.workspace_id)]
    assert (path / "partial.py").read_text() == "original_change=True\n"
    assert len(list((tmp_path / "host-workspaces").iterdir())) == 1
    assert sink.create_calls == publisher.calls == 1
    # Repeated watch/runtime passes must not duplicate an AgentRun, branch or PR.
    assert runtime.run_once().task_status is TaskStatus.WAITING_HUMAN
    assert len(runs.list_runs(task.task_id)) == 2
    assert sink.create_calls == publisher.calls == 1


def test_cli_recover_timeout_is_explicit_and_does_not_dispatch(
    timeout_case, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from factory import __main__ as cli
    from factory.infrastructure.config import FactoryConfig
    from tests.test_runtime import FakeIssueSource

    _, task, run, root, tasks, runs, _, _, _ = timeout_case
    env = {
        "DATABASE_URL": f"sqlite:///{tasks.path}",
        "FACTORY_GITHUB_REPO": "example/control",
        "FACTORY_TARGET_REPO": "example/target",
        "FACTORY_SOURCE_CHECKOUT": str(root),
        "FACTORY_WORKSPACE_ROOT": str(root),
        "GITHUB_TOKEN": "fake-read-token",
        "GITHUB_WRITE_TOKEN": "fake-write-token",
    }
    config = FactoryConfig.from_env(env)
    monkeypatch.setattr(cli.FactoryConfig, "from_env", lambda: config)
    monkeypatch.setattr(cli, "GitHubIssueSource", lambda *a, **kw: FakeIssueSource(task))
    monkeypatch.setattr(cli, "GitHubPullRequestSink", lambda *a, **kw: FakePullRequestSink())
    monkeypatch.setattr(
        cli, "GitWorktreeWorkspaceProvisioner", lambda *a, **kw: FakeWorkspaceProvisioner()
    )
    argv = ["recover-timeout", "--task-id", task.task_id, "--run-id", run.run_id]
    assert cli.main(argv) == cli.EXIT_INTAKE_ERROR
    assert tasks.get(task.task_id).status is TaskStatus.FAILED
    assert "acknowledge-timeout" in capsys.readouterr().out
    assert cli.main([*argv, "--acknowledge-timeout"]) == cli.EXIT_OK
    output = capsys.readouterr().out
    assert "BLOCKED" in output
    assert "fake-read-token" not in output
    assert "fake-write-token" not in output
    assert len(runs.list_runs(task.task_id)) == 1
    assert cli.main([*argv, "--acknowledge-timeout"]) == cli.EXIT_INTAKE_ERROR
    assert len(tasks.history(task.task_id)) == 1
