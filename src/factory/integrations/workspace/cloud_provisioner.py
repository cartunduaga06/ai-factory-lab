"""Local validation workspace provisioning for OpenHands Cloud runs.

A Cloud run still gets a normal isolated local worktree so the factory can bind
and validate the exact remotely-produced revision before publication. Git worktrees
share repository configuration with their source checkout, so this provisioner
must never rewrite `origin` from inside a worktree.
"""

from __future__ import annotations

from factory.integrations.workspace.git import (
    DEFAULT_TIMEOUT_SECONDS,
    GitWorktreeWorkspaceProvisioner,
)


class CloudWorkspaceProvisioner(GitWorktreeWorkspaceProvisioner):
    """Normal isolated worktree provisioning with no shared Git-config mutation."""

    def __init__(
        self,
        source_checkout: str,
        *,
        base_ref: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        super().__init__(source_checkout, base_ref=base_ref, timeout=timeout)

    def __repr__(self) -> str:
        return f"CloudWorkspaceProvisioner(source_checkout={str(self._source)!r})"


__all__ = ["CloudWorkspaceProvisioner"]
