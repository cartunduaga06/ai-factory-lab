"""Read-only exact Git identities for post-rebase revalidation."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from factory.domain.errors import WorkspaceRevisionError
from factory.domain.models import Workspace
from factory.domain.ports import WorkspaceRevalidationInspector
from factory.domain.revalidation import WorkspaceRevisionSnapshot


class GitWorkspaceRevalidationInspector(WorkspaceRevalidationInspector):
    """Require a clean checkout and return immutable commit/tree identities."""

    def snapshot(self, workspace: Workspace) -> WorkspaceRevisionSnapshot:
        root = Path(workspace.path).expanduser()
        if not root.is_dir() or not (root / ".git").exists():
            raise WorkspaceRevisionError(workspace.workspace_id)
        status = self._git(root, "status", "--porcelain")
        if status.strip():
            raise WorkspaceRevisionError(workspace.workspace_id)
        head = self._git(root, "rev-parse", "HEAD").strip()
        tree = self._git(root, "rev-parse", "HEAD^{tree}").strip()
        if len(head) not in {40, 64} or len(tree) not in {40, 64}:
            raise WorkspaceRevisionError(workspace.workspace_id)
        return WorkspaceRevisionSnapshot(head, tree)

    @staticmethod
    def _git(root: Path, *args: str) -> str:
        env = {
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")
            if key in os.environ
        }
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_TERMINAL_PROMPT="0",
        )
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise WorkspaceRevisionError(root.name) from None
        if result.returncode != 0:
            raise WorkspaceRevisionError(root.name)
        return result.stdout


__all__ = ["GitWorkspaceRevalidationInspector"]
