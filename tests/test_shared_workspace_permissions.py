"""Simulate foreign-owned nodes by denying chmod on selected inodes."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from factory.domain.enums import RunStatus, ValidationOutcome
from factory.domain.errors import WorkspaceProvisioningError
from factory.integrations.workspace.git import GitWorktreeWorkspaceProvisioner
from factory.integrations.workspace.revision import GitWorkspaceRevisionInspector
from factory.orchestration.tracking import RunTrackingService
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_workspace import FakeQualityGateRunner, specs
from tests.test_validated_revision import _repos, _setup


def _deny_chmod(monkeypatch: pytest.MonkeyPatch, nodes: list[Path]) -> list[tuple[int, int]]:
    foreign = {(node.stat().st_dev, node.stat().st_ino) for node in nodes}
    attempted: list[tuple[int, int]] = []
    original = os.fchmod

    def chmod(fd: int, mode: int) -> None:
        info = os.fstat(fd)
        identity = (info.st_dev, info.st_ino)
        if identity in foreign:
            attempted.append(identity)
            raise PermissionError("foreign inode cannot be chmodded")
        original(fd, mode)

    monkeypatch.setattr(os, "fchmod", chmod)
    return attempted


@pytest.mark.parametrize(
    ("name", "mode", "directory"),
    [
        ("regular", 0o660, False),
        ("executable", 0o770, False),
        (".env", 0o600, False),
        ("private-script", 0o700, False),
        ("directory", 0o2770, True),
        ("private", 0o700, True),
    ],
)
def test_compliant_foreign_node_skips_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, mode: int, directory: bool
) -> None:
    source, _, task, run = _setup(tmp_path)
    assert run.workspace is not None
    root = Path(run.workspace.path)
    (root / ".gitignore").write_text(".env\nprivate-script\nprivate/\n")
    node = root / name
    if directory:
        node.mkdir()
        if name == "private":
            (node / "nested").mkdir()
            (node / "nested" / "secret").write_text("test-only")
    else:
        node.write_text("test-only")
    node.chmod(mode)
    provisioner = GitWorktreeWorkspaceProvisioner(str(source))
    provisioner.prepare(task, run.workspace)
    attempted = _deny_chmod(monkeypatch, [node])

    assert provisioner.repair(task, run.workspace) == run.workspace
    assert attempted == []
    assert stat.S_IMODE(node.stat().st_mode) == mode
    if name == "private":
        assert stat.S_IMODE((node / "nested").stat().st_mode) == 0o700
        assert stat.S_IMODE((node / "nested" / "secret").stat().st_mode) == 0o600


@pytest.mark.parametrize("directory", [False, True])
def test_noncompliant_foreign_node_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: bool
) -> None:
    source, _, task, run = _setup(tmp_path)
    assert run.workspace is not None
    provisioner = GitWorktreeWorkspaceProvisioner(str(source))
    provisioner.prepare(task, run.workspace)
    node = Path(run.workspace.path) / "foreign"
    if directory:
        node.mkdir()
        node.chmod(0o2750)
    else:
        node.write_text("test-only")
        node.chmod(0o640)
    before = node.stat().st_mode
    attempted = _deny_chmod(monkeypatch, [node])

    with pytest.raises(WorkspaceProvisioningError) as caught:
        provisioner.repair(task, run.workspace)
    assert len(attempted) == 1
    assert node.stat().st_mode == before
    assert caught.value.__context__ is None
    assert "foreign inode" not in str(caught.value)

    tasks, runs, _ = _repos(str(tmp_path / "factory.db"))
    runner = FakeQualityGateRunner()
    result = RunTrackingService(
        tasks,
        runs,
        gate_specs=specs("tests"),
        gate_runner=runner,
        provisioner=provisioner,
        revision_inspector=GitWorkspaceRevisionInspector(),
    ).refresh(run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED))
    assert result.outcome is ValidationOutcome.GATES_FAILED
    assert result.run.validated_revision is None
    assert result.run.gates[0].required
    assert runner.calls == []


def test_compliant_foreign_workspace_binds_revision_without_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _, task, run = _setup(tmp_path)
    assert run.workspace is not None
    root = Path(run.workspace.path)
    (root / ".gitignore").write_text(".env\n")
    (root / "agent.md").write_text("new agent file")
    script = root / "agent.sh"
    script.write_text("#!/bin/sh\nexit 0\n")
    script.chmod(0o770)
    secret = root / ".env"
    secret.write_text("test-only")
    secret.chmod(0o600)
    outside = tmp_path / "outside"
    outside.write_text("unchanged")
    outside.chmod(0o600)
    (root / "link").symlink_to(outside)
    (root / "directory-link").symlink_to(source, target_is_directory=True)
    provisioner = GitWorktreeWorkspaceProvisioner(str(source))
    provisioner.prepare(task, run.workspace)
    nodes = [root, *[node for node in root.rglob("*") if not node.is_symlink()]]
    attempted = _deny_chmod(monkeypatch, nodes)
    inspector = GitWorkspaceRevisionInspector()
    expected = inspector.fingerprint(run.workspace)
    tasks, runs, _ = _repos(str(tmp_path / "factory.db"))
    result = RunTrackingService(
        tasks,
        runs,
        gate_specs=specs("tests"),
        gate_runner=FakeQualityGateRunner(),
        provisioner=provisioner,
        revision_inspector=inspector,
    ).refresh(run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED))
    assert result.outcome is ValidationOutcome.READY_FOR_NEXT_PHASE
    assert result.run.validated_revision == expected
    assert attempted == []
    assert stat.S_IMODE(outside.stat().st_mode) == 0o600
    assert stat.S_IMODE(secret.stat().st_mode) == 0o600
