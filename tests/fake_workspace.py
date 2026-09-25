"""Real (non-mock) workspace and gate doubles for orchestration tests.

These are self-contained implementations of the domain ports, not mocks of the
code under test. They let orchestration be exercised deterministically without a
real Git repository or a real subprocess, while the concrete Git worktree and
local gate runner have their own dedicated tests against real temporary
repositories.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path

from factory.domain.enums import QualityGateStatus
from factory.domain.errors import WorkspaceProvisioningError, WorkspaceRevisionError
from factory.domain.models import FactoryTask, QualityGate, QualityGateSpec, Workspace
from factory.domain.ports import (
    QualityGateRunner,
    WorkspaceProvisioner,
    WorkspaceRevisionInspector,
)


class FakeWorkspaceProvisioner(WorkspaceProvisioner):
    """Creates a real directory for each workspace, on a per-workspace branch.

    Records what it prepared so tests can assert preparation happened *before*
    the adapter ran. ``fail_with`` lets a test drive the provisioning-failure
    path; the exception is turned into a sanitized factory error just like the
    concrete provisioner does.
    """

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self._fail_with = fail_with
        self.prepared: list[tuple[str, str, str]] = []

    def prepare(self, task: FactoryTask, workspace: Workspace) -> Workspace:
        del task
        if self._fail_with is not None:
            raise WorkspaceProvisioningError(workspace.workspace_id) from None
        Path(workspace.path).mkdir(parents=True, exist_ok=True)
        self.prepared.append((workspace.workspace_id, workspace.branch, workspace.path))
        return workspace


def specs(*names: str) -> tuple[QualityGateSpec, ...]:
    """Build required gate specs named ``names`` with a trivial argv."""
    return tuple(QualityGateSpec(name=name, argv=("true",)) for name in names)


class FakeQualityGateRunner(QualityGateRunner):
    """Returns scripted gate statuses, keyed by gate name.

    Records the workspaces each gate ran in, so a test can assert gates ran
    inside the run's own workspace. ``on_run`` lets a test mutate the workspace
    while a gate executes, to drive the revision-mutation path.
    """

    def __init__(
        self,
        statuses: dict[str, QualityGateStatus] | None = None,
        *,
        on_run: Callable[[str, Workspace], None] | None = None,
    ) -> None:
        self._statuses = statuses or {}
        self._on_run = on_run
        self.calls: list[tuple[str, str]] = []

    def run(self, spec: QualityGateSpec, workspace: Workspace) -> QualityGate:
        self.calls.append((spec.name, workspace.path))
        if self._on_run is not None:
            self._on_run(spec.name, workspace)
        status = self._statuses.get(spec.name, QualityGateStatus.PASSED)
        return QualityGate(
            name=spec.name,
            status=status,
            detail="exit_code=0" if status is QualityGateStatus.PASSED else "exit_code=1",
            required=spec.required,
        )


class FakeRevisionInspector(WorkspaceRevisionInspector):
    """A real (non-mock) revision inspector over a directory's contents.

    It fingerprints a workspace by hashing the sorted (relative path, content)
    pairs of the files under ``Workspace.path`` — enough to prove the binding
    logic deterministically without a real Git repository. Tests that need a
    fixed identity can supply explicit ``revisions`` or a ``revision_factory``.
    """

    def __init__(
        self,
        *,
        revisions: dict[str, str] | None = None,
        revision_factory: Callable[[Workspace], str] | None = None,
    ) -> None:
        self._revisions = revisions or {}
        self._revision_factory = revision_factory
        self.calls: list[str] = []

    def fingerprint(self, workspace: Workspace) -> str:
        self.calls.append(workspace.workspace_id)
        if workspace.workspace_id in self._revisions:
            return self._revisions[workspace.workspace_id]
        if self._revision_factory is not None:
            return self._revision_factory(workspace)
        root = Path(workspace.path)
        if not root.is_dir():
            raise WorkspaceRevisionError(workspace.workspace_id)
        digest = hashlib.sha256()
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            digest.update(path.relative_to(root).as_posix().encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()


def optional_spec(name: str) -> QualityGateSpec:
    """Build an optional gate spec named ``name``."""
    return QualityGateSpec(name=name, argv=("true",), required=False)


def statuses(**kwargs: QualityGateStatus) -> dict[str, QualityGateStatus]:
    return dict(kwargs)


__all__ = [
    "FakeQualityGateRunner",
    "FakeRevisionInspector",
    "FakeWorkspaceProvisioner",
    "optional_spec",
    "specs",
    "statuses",
]
