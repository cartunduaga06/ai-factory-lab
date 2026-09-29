"""Host-local scratch capability and deterministic operational acceptance."""

from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from factory.domain.enums import QualityGateStatus, TaskKind
from factory.domain.errors import WorkspaceProvisioningError
from factory.domain.models import AgentRun, FactoryTask, QualityGate, Workspace
from factory.domain.operational import parse_scratch_artifact
from factory.domain.ports import OperationalAcceptance, WorkspaceProvisioner


class ScratchWorkspaceProvisioner(WorkspaceProvisioner):
    """Create only private per-run directories under an operator owned root."""

    def __init__(self, root: str) -> None:
        self._root = Path(root)

    def prepare(self, task: FactoryTask, workspace: Workspace) -> Workspace:
        self._check(task, workspace, must_exist=False)
        try:
            Path(workspace.path).mkdir(mode=0o700)
        except OSError:
            raise WorkspaceProvisioningError(workspace.workspace_id) from None
        return self.repair(task, workspace)

    def repair(self, task: FactoryTask, workspace: Workspace) -> Workspace:
        self._check(task, workspace, must_exist=True)
        return workspace

    def _check(self, task: FactoryTask, workspace: Workspace, *, must_exist: bool) -> None:
        try:
            root = self._root
            info = root.lstat()
            if (
                os.geteuid() == 0
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or root.is_symlink()
                or not root.is_absolute()
                or root.resolve() != root
                or workspace.kind is not TaskKind.OPERATIONAL
                or task.kind is not TaskKind.OPERATIONAL
                or Path(workspace.path).parent != root
                or Path(workspace.path).name != workspace.workspace_id
                or workspace.repository_slug != task.target_repository
            ):
                raise ValueError("unsafe scratch capability")
            if must_exist:
                child = Path(workspace.path).lstat()
                if (
                    not stat.S_ISDIR(child.st_mode)
                    or child.st_uid != os.geteuid()
                    or child.st_mode & 0o077
                ):
                    raise ValueError("unsafe scratch workspace")
        except (OSError, ValueError):
            raise WorkspaceProvisioningError(workspace.workspace_id) from None


class ScratchAcceptance(OperationalAcceptance):
    """Verify exact bytes in the declared artifact without executing issue commands."""

    def __init__(self, provisioner: ScratchWorkspaceProvisioner) -> None:
        self._provisioner = provisioner

    def validate(self, task: FactoryTask, run: AgentRun) -> tuple[QualityGate, ...]:
        workspace = run.workspace
        size_detail = "mismatch"
        digest_detail = "mismatch"
        try:
            if workspace is None:
                raise ValueError("no workspace")
            self._provisioner.repair(task, workspace)
            declaration = parse_scratch_artifact(task)
            directory = os.open(workspace.path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                descriptor = os.open(
                    declaration.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory
                )
                try:
                    info = os.fstat(descriptor)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError("artifact is not a regular file")
                    data = os.read(descriptor, 4097) if info.st_size <= 4096 else b""
                finally:
                    os.close(descriptor)
            finally:
                os.close(directory)
            size_ok = len(data) == declaration.size
            digest_ok = size_ok and hashlib.sha256(data).hexdigest() == declaration.sha256
            if size_ok:
                size_detail = f"bytes={declaration.size}"
            if digest_ok:
                digest_detail = f"sha256={declaration.sha256}"
        except (OSError, ValueError, WorkspaceProvisioningError):
            size_ok = False
            digest_ok = False
        return (
            QualityGate("artifact_size", _status(size_ok), size_detail),
            QualityGate("artifact_sha256", _status(digest_ok), digest_detail),
        )


def _status(passed: bool) -> QualityGateStatus:
    return QualityGateStatus.PASSED if passed else QualityGateStatus.FAILED
