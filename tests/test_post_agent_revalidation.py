"""Post-agent repair and bounded legacy recovery, using disposable worktrees."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from factory.domain.enums import QualityGateStatus, RunStatus, TaskStatus, ValidationOutcome
from factory.domain.errors import WorkspaceProvisioningError
from factory.domain.models import AgentRun, FactoryTask, QualityGate, Workspace
from factory.integrations.workspace.git import GitWorktreeWorkspaceProvisioner
from factory.integrations.workspace.revision import GitWorkspaceRevisionInspector
from factory.orchestration.tracking import RunTrackingService
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink
from tests.fake_workspace import FakeQualityGateRunner, FakeRevisionInspector, specs
from tests.test_validated_revision import _git, _repos, _setup


def test_post_agent_repair_preserves_content_modes_secrets_and_links(tmp_path: Path) -> None:
    source, _, task, run = _setup(tmp_path)
    assert run.workspace is not None
    workspace = run.workspace
    root = Path(workspace.path)
    provisioner = GitWorktreeWorkspaceProvisioner(str(source))
    provisioner.prepare(task, workspace)
    (root / ".gitignore").write_text(".env\n")
    (root / "docs").mkdir(mode=0o700)
    regular = root / "docs" / "agent.md"
    regular.write_text("agent result\n")
    regular.chmod(0o600)
    executable = root / "agent.sh"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o700)
    secret = root / ".env"
    secret.write_text("TEST_ONLY=private\n")
    secret.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    outside.chmod(0o600)
    (root / "link").symlink_to(outside)
    (root / "directory-link").symlink_to(source, target_is_directory=True)
    source_mode = (source / "README.md").stat().st_mode
    before = GitWorkspaceRevisionInspector().fingerprint(workspace)
    index = _git(root, "ls-files", "--stage").stdout

    runner = FakeQualityGateRunner()
    tasks, runs, _ = _repos(str(tmp_path / "factory.db"))
    service = RunTrackingService(
        tasks,
        runs,
        gate_specs=specs("tests"),
        gate_runner=runner,
        revision_inspector=GitWorkspaceRevisionInspector(),
        provisioner=provisioner,
    )
    result = service.refresh(run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED))

    assert result.outcome is ValidationOutcome.READY_FOR_NEXT_PHASE
    assert result.run.validated_revision == before
    assert stat.S_IMODE(regular.stat().st_mode) == 0o660
    assert stat.S_IMODE(executable.stat().st_mode) == 0o770
    assert stat.S_IMODE((root / "docs").stat().st_mode) == 0o2770
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    assert regular.read_text() == "agent result\n"
    assert (source / "README.md").stat().st_mode == source_mode
    assert _git(root, "ls-files", "--stage").stdout == index
    assert _git(root, "branch", "--show-current").stdout.decode().strip() == workspace.branch
    assert _git(source, "branch", "--show-current").stdout.decode().strip() == "main"
    assert runner.calls == [("tests", workspace.path)]


@pytest.mark.parametrize("unsafe", ["missing", "branch", "hardlink", "fifo", "symlink"])
def test_repair_refuses_unsafe_workspace_without_creation(tmp_path: Path, unsafe: str) -> None:
    source, _, task, run = _setup(tmp_path)
    assert run.workspace is not None
    workspace = run.workspace
    root = Path(workspace.path)
    if unsafe == "missing":
        workspace = Workspace(
            repository_slug=task.target_repository,
            branch=workspace.branch,
            path=str(tmp_path / "missing"),
        )
    elif unsafe == "branch":
        workspace = Workspace(
            repository_slug=task.target_repository,
            branch="factory/wrong/branch",
            path=workspace.path,
        )
    elif unsafe == "hardlink":
        os.link(root / "README.md", root / "hardlink")
    elif unsafe == "fifo":
        os.mkfifo(root / "fifo")
    else:
        link = tmp_path / "symlink"
        link.symlink_to(root, target_is_directory=True)
        workspace = Workspace(
            repository_slug=task.target_repository, branch=workspace.branch, path=str(link)
        )
    with pytest.raises(WorkspaceProvisioningError):
        GitWorktreeWorkspaceProvisioner(str(source)).repair(task, workspace)
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("failure", ["repair", "before", "after"])
def test_validation_failures_record_sanitized_required_gate(tmp_path: Path, failure: str) -> None:
    source, _, task, run = _setup(tmp_path)
    assert run.workspace is not None
    tasks, runs, _ = _repos(str(tmp_path / "factory.db"))
    count = 0

    def inspect(workspace: Workspace) -> str:
        nonlocal count
        count += 1
        if count == (1 if failure == "before" else 2):
            raise RuntimeError("SECRET /private/path raw git output")
        return "tree"

    if failure == "repair":
        os.mkfifo(Path(run.workspace.path) / "fifo")
    runner = FakeQualityGateRunner()
    service = RunTrackingService(
        tasks,
        runs,
        gate_specs=specs("tests"),
        gate_runner=runner,
        provisioner=GitWorktreeWorkspaceProvisioner(str(source)),
        revision_inspector=FakeRevisionInspector(revision_factory=inspect),
    )
    result = service.refresh(run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED))
    assert result.outcome is ValidationOutcome.GATES_FAILED
    assert result.run.validated_revision is None
    gate = result.run.gates[-1]
    assert gate.name == "workspace_integrity" and gate.required
    assert gate.status is QualityGateStatus.FAILED
    assert gate.detail in {"workspace repair failed", "workspace inspection failed"}
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    stored = runs.get_run(run.run_id)
    assert stored is not None and stored.gates == result.run.gates
    assert len(runner.calls) == (1 if failure == "after" else 0)


@pytest.mark.parametrize(
    "case",
    [
        "recover",
        "failed",
        "superseded",
        "bound",
        "remote_pr",
        "persisted_pr",
        "mutation",
        "gate_failure",
    ],
)
def test_legacy_recovery_is_guarded_and_reuses_run(tmp_path: Path, case: str) -> None:
    source, _, task, run = _setup(tmp_path)
    tasks, runs, prs = _repos(str(tmp_path / "factory.db"))
    assert run.workspace is not None
    run.status = RunStatus.SUCCEEDED
    run.gates = (
        QualityGate(
            "tests", QualityGateStatus.FAILED if case == "failed" else QualityGateStatus.PASSED
        ),
    )
    run.validated_revision = "already-bound" if case == "bound" else None
    runs.update_run(run)
    tasks.apply_transition(task.task_id, TaskStatus.RUNNING, TaskStatus.VALIDATING)
    if case == "superseded":
        runs.save_run(AgentRun(task_id=task.task_id, adapter=run.adapter, status=RunStatus.FAILED))
    sink = FakePullRequestSink()
    if case in {"remote_pr", "persisted_pr"}:
        from factory.domain.models import PullRequest

        existing = sink.open_pull_request(
            PullRequest(
                task_id=task.task_id,
                run_id=run.run_id,
                repository_slug=task.target_repository,
                head_branch=run.workspace.branch,
                base_branch="main",
                title="Existing",
                body="",
            )
        )

    if case == "persisted_pr":
        prs.save(existing)

    def mutate(name: str, workspace: Workspace) -> None:
        if case == "mutation":
            (Path(workspace.path) / "changed").write_text(name)

    runner = FakeQualityGateRunner(
        statuses={"tests": QualityGateStatus.FAILED} if case == "gate_failure" else {},
        on_run=mutate,
    )
    adapter = FakeAgentAdapter()
    service = RunTrackingService(
        tasks,
        runs,
        gate_specs=specs("tests", "lint"),
        gate_runner=runner,
        provisioner=GitWorktreeWorkspaceProvisioner(str(source)),
        revision_inspector=GitWorkspaceRevisionInspector(),
        pull_requests=prs,
        pull_request_sink=sink,
    )
    run_ids = [item.run_id for item in runs.list_runs()]
    first = service.refresh(run.run_id, adapter)
    calls = list(runner.calls)
    second = service.refresh(run.run_id, adapter)
    assert runner.calls == calls
    assert adapter.dispatched == [] and adapter.collected == 0
    assert [item.run_id for item in runs.list_runs()] == run_ids
    assert first.run.workspace == second.run.workspace == run.workspace
    assert runs.get_workspace(run.workspace.workspace_id) == run.workspace
    if case in {"recover", "mutation", "gate_failure"}:
        assert [name for name, _ in calls] == ["tests", "lint"]
        assert (first.run.validated_revision is not None) == (case == "recover")
        assert first.outcome is (
            ValidationOutcome.READY_FOR_NEXT_PHASE
            if case == "recover"
            else ValidationOutcome.GATES_FAILED
        )
    else:
        assert calls == []
        assert first.run.validated_revision == run.validated_revision
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_repair_returning_different_identity_fails_closed(tmp_path: Path) -> None:
    source, _, task, run = _setup(tmp_path)
    tasks, runs, _ = _repos(str(tmp_path / "factory.db"))

    class WrongIdentityProvisioner(GitWorktreeWorkspaceProvisioner):
        def repair(self, task: FactoryTask, workspace: Workspace) -> Workspace:
            return Workspace(
                repository_slug=task.target_repository,
                branch="factory/wrong/branch",
                path=workspace.path,
            )

    runner = FakeQualityGateRunner()
    result = RunTrackingService(
        tasks,
        runs,
        gate_specs=specs("tests"),
        gate_runner=runner,
        provisioner=WrongIdentityProvisioner(str(source)),
        revision_inspector=GitWorkspaceRevisionInspector(),
    ).refresh(run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED))
    assert result.outcome is ValidationOutcome.GATES_FAILED
    assert result.run.validated_revision is None
    assert result.run.gates[0].detail == "workspace identity mismatch"
    assert runner.calls == []
