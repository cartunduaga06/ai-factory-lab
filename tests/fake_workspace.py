"""Real (non-mock) workspace and gate doubles for orchestration tests.

These are self-contained implementations of the domain ports, not mocks of the
code under test. They let orchestration be exercised deterministically without a
real Git repository or a real subprocess, while the concrete Git worktree and
local gate runner have their own dedicated tests against real temporary
repositories.
"""

from __future__ import annotations

from pathlib import Path

from factory.domain.enums import QualityGateStatus
from factory.domain.errors import WorkspaceProvisioningError
from factory.domain.models import FactoryTask, QualityGate, QualityGateSpec, Workspace
from factory.domain.ports import QualityGateRunner, WorkspaceProvisioner


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


class FakeQualityGateRunner(QualityGateRunner):
    """Returns scripted gate statuses, keyed by gate name.

    Records the workspaces each gate ran in, so a test can assert gates ran
    inside the run's own workspace.
    """

    def __init__(self, statuses: dict[str, QualityGateStatus] | None = None) -> None:
        self._statuses = statuses or {}
        self.calls: list[tuple[str, str]] = []

    def run(self, spec: QualityGateSpec, workspace: Workspace) -> QualityGate:
        self.calls.append((spec.name, workspace.path))
        status = self._statuses.get(spec.name, QualityGateStatus.PASSED)
        return QualityGate(
            name=spec.name,
            status=status,
            detail="exit_code=0" if status is QualityGateStatus.PASSED else "exit_code=1",
            required=spec.required,
        )


def specs(*names: str) -> tuple[QualityGateSpec, ...]:
    """Build required gate specs named ``names`` with a trivial argv."""
    return tuple(QualityGateSpec(name=name, argv=("true",)) for name in names)


def optional_spec(name: str) -> QualityGateSpec:
    return QualityGateSpec(name=name, argv=("true",), required=False)


def statuses(**kwargs: QualityGateStatus) -> dict[str, QualityGateStatus]:
    return dict(kwargs)


__all__ = [
    "FakeQualityGateRunner",
    "FakeWorkspaceProvisioner",
    "optional_spec",
    "specs",
    "statuses",
]
