"""Offline operational intake through watcher and Codex worker."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from factory.__main__ import _build_runtime
from factory.domain.enums import AgentKind, QualityGateStatus, RepositoryRole, TaskKind, TaskStatus
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    Repository,
    TaskSource,
    new_operational_workspace,
)
from factory.domain.operational import parse_scratch_artifact
from factory.infrastructure.config import FactoryConfig
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.integrations.codex import CodexAdapter
from factory.integrations.github.client import GitHubClient
from factory.integrations.github.issues import GitHubIssueSource
from factory.integrations.operational import ScratchAcceptance, ScratchWorkspaceProvisioner
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.runtime import FactoryRuntime
from factory.orchestration.watch import FactoryWatcher
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeQualityGateRunner,
    FakeRevisionInspector,
    FakeWorkspaceProvisioner,
)


class IssueSource:
    def __init__(self, task: FactoryTask) -> None:
        self.task = task

    def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
        del repository
        return [self.task]

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        del repository, source
        return self.task

    def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
        del repository
        return source == self.task.source


class IssueTransport:
    """In-memory GitHub API response for the actual GitHub issue adapter."""

    def __init__(self, task: FactoryTask) -> None:
        assert task.source is not None
        self.issue = {
            "number": task.source.issue_number,
            "title": task.title,
            "body": task.body,
            "state": "open",
            "labels": [{"name": name} for name in task.labels],
        }

    def get_json(self, url: str, headers: Mapping[str, str]) -> Any:  # noqa: ANN401
        del headers
        return [self.issue] if "?" in url else self.issue


def _task(*, digest: str | None = None, mode: str = "scratch_artifact") -> FactoryTask:
    payload = b"factory-operational-proof\n"
    declaration = {
        "mode": mode,
        "risk": "low",
        "artifact": "proof.txt",
        "payload_hex": payload.hex(),
        "sha256": digest or hashlib.sha256(payload).hexdigest(),
    }
    return FactoryTask(
        title="Scratch proof",
        target_repository="example/control",
        source=TaskSource("github", "example/control", 36),
        kind=TaskKind.OPERATIONAL,
        labels=("factory-ready", "factory-operational"),
        body="```factory-operational\n" + json.dumps(declaration) + "\n```",
    )


def _runtime(
    tmp_path: Path,
    task: FactoryTask,
    executable: str | None,
    *,
    github: bool = False,
    code_capable: bool = True,
) -> tuple[
    FactoryRuntime,
    SqliteTaskRepository,
    SqliteRunRepository,
    FakeWorkspacePublisher,
    FakePullRequestSink,
]:
    database = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(database)
    runs = SqliteRunRepository(database)
    prs = SqlitePullRequestRepository(database)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    root = tmp_path / "scratch"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    provisioner = ScratchWorkspaceProvisioner(str(root))
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()
    source = (
        GitHubIssueSource(
            GitHubClient("test-token", "https://api.github.com", transport=IssueTransport(task))
        )
        if github
        else IssueSource(task)
    )
    runtime = FactoryRuntime(
        intake=IssueIntakeService(source, tasks),
        intake_repository=Repository("example/control", RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=CodexAdapter(executable=executable or "missing-codex", timeout=3),
        provisioner=FakeWorkspaceProvisioner(),
        workspace_root=str(tmp_path / "code"),
        gate_specs=(),
        gate_runner=FakeQualityGateRunner(),
        revision_inspector=FakeRevisionInspector(),
        publisher=publisher,
        pull_request_sink=sink,
        pull_requests=prs,
        base_branch="main",
        operational_provisioner=provisioner,
        operational_root=str(root),
        operational_acceptance=ScratchAcceptance(provisioner),
        code_capable=code_capable,
        poll_interval=0.01,
        timeout=4,
    )
    return runtime, tasks, runs, publisher, sink


def _fake_codex(tmp_path: Path, *, write: bool = True, corrupt: bool = False) -> str:
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "args = sys.argv[1:]\n"
        "assert args[:4] == ['exec', '--sandbox', 'workspace-write', '--cd']\n"
        "instruction = sys.stdin.read()\n"
        "assert 'File name: proof.txt' in instruction\n"
        "assert 'Scratch proof' not in instruction\n"
        + (
            "name = instruction.split('File name: ')[1].splitlines()[0]\n"
            "payload = instruction.split('Bytes (hex): ')[1].splitlines()[0]\n"
            "data = bytes.fromhex(payload)\n"
            + ("data = b'X' + data[1:]\n" if corrupt else "")
            + "pathlib.Path(name).write_bytes(data)\n"
            if write
            else ""
        )
        + "pathlib.Path(args[6]).write_text('complete')\n"
    )
    executable.chmod(0o755)
    return str(executable)


def test_manifest_blocks_non_scratch_and_bad_checksum() -> None:
    for task in (_task(mode="delete_volume"), _task(digest="0" * 64)):
        try:
            parse_scratch_artifact(task)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe declaration accepted")


def test_watch_codex_scratch_acceptance_without_pr(tmp_path: Path) -> None:
    task = _task()
    task.body = "untrusted prose must not persist\n" + task.body
    runtime, tasks, runs, publisher, sink = _runtime(
        tmp_path, task, _fake_codex(tmp_path), github=True
    )
    outcome = FactoryWatcher(runtime=runtime, idle_interval=0, max_iterations=2).run()
    result = outcome.last_result
    assert result is not None
    assert outcome.processed == 1
    assert result.outcome == "NO_ELIGIBLE_TASK"
    completed = tasks.list(TaskStatus.DONE)
    assert len(completed) == 1
    stored = completed[0]
    assert stored.kind is TaskKind.OPERATIONAL
    assert "untrusted prose" not in stored.body
    assert stored.status is TaskStatus.DONE
    run = runs.list_runs(stored.task_id)[0]
    assert run.workspace is not None and run.workspace.branch == ""
    assert {gate.status for gate in run.gates} == {QualityGateStatus.PASSED}
    assert any(gate.detail == "bytes=26" for gate in run.gates)
    expected_detail = "sha256=" + hashlib.sha256(b"factory-operational-proof\n").hexdigest()
    assert any(gate.detail == expected_detail for gate in run.gates)
    assert run.summary is not None and "exit_code=0" in run.summary
    assert publisher.calls == sink.create_calls == 0


def test_new_operational_issue_runs_while_code_awaits_human(tmp_path: Path) -> None:
    operational = _task()
    operational.source = TaskSource("github", "example/control", 39)
    runtime, tasks, runs, publisher, sink = _runtime(
        tmp_path, operational, _fake_codex(tmp_path), github=True
    )
    code = tasks.save(
        FactoryTask(
            title="Code PR awaiting review",
            target_repository="example/target",
            source=TaskSource("github", "example/control", 36),
            kind=TaskKind.CODE,
        )
    )
    for status in (
        TaskStatus.READY,
        TaskStatus.CLAIMED,
        TaskStatus.RUNNING,
        TaskStatus.VALIDATING,
        TaskStatus.PR_OPEN,
        TaskStatus.WAITING_HUMAN,
    ):
        runtime._dispatch.lifecycle.transition(code.task_id, status)

    summary = runtime._intake.intake(runtime._intake_repository)
    discovered = tasks.find_by_source(operational.source)
    assert summary.created == 1
    assert discovered is not None and discovered.status is TaskStatus.DISCOVERED
    assert tasks.history(discovered.task_id) == []

    result = runtime.run_once()

    assert result.task_id == discovered.task_id
    assert result.outcome == "OPERATIONAL_DONE"
    assert tasks.get(discovered.task_id).status is TaskStatus.DONE
    assert [(t.from_status, t.to_status) for t in tasks.history(discovered.task_id)][:2] == [
        (TaskStatus.DISCOVERED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
    ]
    assert len(runs.list_runs(discovered.task_id)) == 1
    assert tasks.get(code.task_id).status is TaskStatus.WAITING_HUMAN
    assert publisher.calls == sink.create_calls == 0


def test_agent_exit_zero_without_artifact_blocks(tmp_path: Path) -> None:
    task = _task()
    runtime, tasks, runs, publisher, sink = _runtime(
        tmp_path, task, _fake_codex(tmp_path, write=False)
    )
    result = runtime.run_once()
    assert result.task_status is TaskStatus.BLOCKED
    assert result.outcome == "QUALITY_GATES_FAILED"
    stored = tasks.get(task.task_id)
    assert stored is not None and stored.blocked_reason == "operational acceptance gates failed"
    assert any(
        gate.status is QualityGateStatus.FAILED for gate in runs.list_runs(task.task_id)[0].gates
    )
    assert publisher.calls == sink.create_calls == 0


def test_wrong_checksum_blocks_after_size_pass(tmp_path: Path) -> None:
    task = _task()
    runtime, _, runs, _, _ = _runtime(tmp_path, task, _fake_codex(tmp_path, corrupt=True))
    result = runtime.run_once()
    assert result.task_status is TaskStatus.BLOCKED
    gates = {gate.name: gate.status for gate in runs.list_runs(task.task_id)[0].gates}
    assert gates == {
        "artifact_size": QualityGateStatus.PASSED,
        "artifact_sha256": QualityGateStatus.FAILED,
    }


def test_failed_codex_run_records_failure_without_acceptance(tmp_path: Path) -> None:
    task = _task()
    runtime, tasks, runs, _, _ = _runtime(tmp_path, task, "missing-codex")
    result = runtime.run_once()
    assert result.task_status is TaskStatus.FAILED
    stored = tasks.get(task.task_id)
    assert stored is not None and stored.blocked_reason == "operational agent execution failed"
    assert runs.list_runs(task.task_id)[0].gates == ()


def test_invalid_policy_blocks_before_codex(tmp_path: Path) -> None:
    task = _task(mode="stop_production")
    runtime, tasks, runs, publisher, sink = _runtime(tmp_path, task, "missing-codex")
    result = runtime.run_once()
    assert result.task_status is TaskStatus.BLOCKED
    assert result.outcome == "OPERATIONAL_POLICY_BLOCKED"
    stored = tasks.get(task.task_id)
    assert stored is not None and stored.blocked_reason == "operational policy rejected declaration"
    assert runs.list_runs(task.task_id) == []
    assert publisher.calls == sink.create_calls == 0


def test_invalid_github_operational_body_is_not_persisted(tmp_path: Path) -> None:
    task = _task(mode="delete_volume")
    task.body = "untrusted prose\n" + task.body
    runtime, tasks, runs, _, _ = _runtime(tmp_path, task, "missing-codex", github=True)
    result = runtime.run_once()
    assert result.outcome == "OPERATIONAL_POLICY_BLOCKED"
    stored = tasks.list(TaskStatus.BLOCKED)[0]
    assert stored.body == ""
    assert runs.list_runs(stored.task_id) == []


def test_existing_tasks_migrate_to_code_kind(tmp_path: Path) -> None:
    database = tmp_path / "legacy.db"
    with sqlite3.connect(database) as connection:
        connection.execute(
            """CREATE TABLE tasks (
                task_id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL,
                target_repository TEXT NOT NULL, source_provider TEXT,
                source_repository TEXT, source_issue_number INTEGER,
                status TEXT NOT NULL, labels TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """INSERT INTO tasks VALUES
            ('legacy','old','', 'example/control',NULL,NULL,NULL,'READY','[]',
             '2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"""
        )
    tasks = SqliteTaskRepository(str(database))
    tasks.initialize()
    old = tasks.get("legacy")
    assert old is not None and old.kind is TaskKind.CODE


def test_persisted_task_kind_cannot_be_reclassified(tmp_path: Path) -> None:
    tasks = SqliteTaskRepository(str(tmp_path / "tasks.db"))
    tasks.initialize()
    task = _task()
    tasks.save(task)
    task.kind = TaskKind.CODE
    try:
        tasks.update(task)
    except KeyError:
        pass
    else:
        raise AssertionError("stored task kind was silently changed")
    stored = tasks.get(task.task_id)
    assert stored is not None and stored.kind is TaskKind.OPERATIONAL


def test_operational_only_runtime_needs_no_write_credential(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir(mode=0o700)
    config = FactoryConfig.from_env(
        {
            "GITHUB_TOKEN": "test-token",
            "FACTORY_GITHUB_REPO": "example/control",
            "FACTORY_OPERATIONAL_SCRATCH_ROOT": str(root),
            "DATABASE_URL": "sqlite:///" + str(tmp_path / "factory.db"),
        }
    )
    runtime = _build_runtime(config)
    assert runtime._code_capable is False


def test_missing_capability_blocks_before_dispatch(tmp_path: Path) -> None:
    task = _task()
    runtime, tasks, runs, _, _ = _runtime(tmp_path, task, "missing-codex")
    runtime._operational_dispatch = None
    result = runtime.run_once()
    assert result.outcome == "OPERATIONAL_CAPABILITY_MISSING"
    stored = tasks.get(task.task_id)
    assert stored is not None and stored.blocked_reason == "operational capability missing"
    assert runs.list_runs(task.task_id) == []


def test_symlink_artifact_cannot_satisfy_gate(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    root.mkdir(mode=0o700)
    root.chmod(0o700)
    task = _task()
    workspace = new_operational_workspace(task, str(root))
    provisioner = ScratchWorkspaceProvisioner(str(root))
    provisioner.prepare(task, workspace)
    outside = tmp_path / "outside"
    outside.write_bytes(bytes.fromhex(parse_scratch_artifact(task).payload_hex))
    (Path(workspace.path) / "proof.txt").symlink_to(outside)
    run = AgentRun(task_id=task.task_id, adapter=AgentKind.CODEX, workspace=workspace)
    gates = ScratchAcceptance(provisioner).validate(task, run)
    assert all(gate.status is QualityGateStatus.FAILED for gate in gates)


def test_operational_only_runtime_refuses_code_dispatch(tmp_path: Path) -> None:
    task = FactoryTask(
        "Code task",
        "example/control",
        source=TaskSource("github", "example/control", 37),
    )
    runtime, tasks, runs, publisher, sink = _runtime(
        tmp_path, task, "missing-codex", code_capable=False
    )
    result = runtime.run_once()
    assert result.outcome == "CODE_CAPABILITY_MISSING"
    assert result.task_status is TaskStatus.BLOCKED
    assert runs.list_runs(task.task_id) == []
    assert publisher.calls == sink.create_calls == 0


def test_removed_operational_label_cancels_before_dispatch(tmp_path: Path) -> None:
    task = _task()
    runtime, tasks, runs, _, _ = _runtime(tmp_path, task, "missing-codex")
    runtime._intake.intake(runtime._intake_repository)
    task.kind = TaskKind.CODE
    result = runtime.run_once()
    assert result.outcome == "SOURCE_DECLARATION_CHANGED"
    stored = tasks.get(task.task_id)
    assert stored is not None and stored.status is TaskStatus.CANCELLED
    assert runs.list_runs(task.task_id) == []
