"""A workspace provisioner for Cloud runs.

Cloud execution still needs a local, isolated Git worktree: it is where the exact
Cloud revision is materialised into, and where the factory's existing gates,
revision binding and publication run. This provisioner therefore reuses the local
worktree machinery unchanged and adds the one Cloud-specific step — pointing the
new worktree's ``origin`` at the repository Cloud actually cloned, so the
materialiser can fetch the run's branch from it.

Everything else (branch identity, permission policy, retry safety) is inherited
from :class:`~factory.integrations.workspace.git.GitWorktreeWorkspaceProvisioner`.
"""

from __future__ import annotations

from pathlib import Path

from factory.domain.models import FactoryTask, Workspace
from factory.integrations.workspace.git import (
    DEFAULT_TIMEOUT_SECONDS,
    GitWorktreeWorkspaceProvisioner,
)

#: Only GitHub HTTPS remotes are accepted. The URL is validated by construction,
#: never assembled from untrusted input, and carries no credential.
_CLOUD_REPO_PREFIX = "https://github.com/"


class CloudWorkspaceProvisioner(GitWorktreeWorkspaceProvisioner):
    """Local worktree provisioner whose workspace fetches from the Cloud repo."""

    def __init__(
        self,
        source_checkout: str,
        *,
        remote: str,
        base_ref: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not remote.startswith(_CLOUD_REPO_PREFIX) or remote.endswith("/"):
            raise ValueError("cloud remote must be an https://github.com/<owner>/<name> URL")
        super().__init__(source_checkout, base_ref=base_ref, timeout=timeout)
        self._remote = remote

    def __repr__(self) -> str:
        # The remote URL carries no credential, but keep the repr minimal anyway.
        return f"CloudWorkspaceProvisioner(source_checkout={str(self._source)!r})"

    def prepare(self, task: FactoryTask, workspace: Workspace) -> Workspace:
        prepared = super().prepare(task, workspace)
        self._point_remote_at_cloud_repo(prepared)
        return prepared

    def repair(self, task: FactoryTask, workspace: Workspace) -> Workspace:
        repaired = super().repair(task, workspace)
        self._point_remote_at_cloud_repo(repaired)
        return repaired

    def _point_remote_at_cloud_repo(self, workspace: Workspace) -> None:
        """Set the workspace's own ``origin`` to the Cloud repository.

        Only the workspace's local repository configuration is touched; the source
        checkout and any network remote are left alone. A worktree created from a
        source checkout that already has an ``origin`` gets it repointed; one
        without is given one.
        """
        target = Path(workspace.path).expanduser()
        code = self._run(
            ["remote", "set-url", "origin", self._remote],
            cwd=target,
            workspace_id=workspace.workspace_id,
            allow_failure=True,
        )
        if code != 0:
            self._run(
                ["remote", "add", "origin", self._remote],
                cwd=target,
                workspace_id=workspace.workspace_id,
            )


__all__ = ["CloudWorkspaceProvisioner"]
