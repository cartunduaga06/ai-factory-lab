"""Read-only Git evidence for exceptional clean worker recovery."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from factory.domain.models import Workspace

_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
)


def git_workspace_is_clean_unpublished(
    workspace: Workspace, base_ref: str, *, timeout: float = 10.0
) -> bool:
    """Return whether a failed workspace is unchanged and has no commits ahead."""
    if timeout <= 0 or not base_ref or base_ref.startswith("-"):
        return False
    path = Path(workspace.path).expanduser()
    if path.is_symlink() or not path.is_dir():
        return False
    env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}

    def capture(args: list[str]) -> str | None:
        try:
            completed = subprocess.run(  # noqa: S603 - fixed git argv, no shell
                ["git", *args],
                cwd=str(path),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if completed.returncode != 0:
            return None
        return os.fsdecode(completed.stdout).strip()

    branch = capture(["branch", "--show-current"])
    dirty = capture(["status", "--porcelain", "--untracked-files=all"])
    ahead = capture(["rev-list", "--count", f"origin/{base_ref}..HEAD", "--"])
    return branch == workspace.branch and dirty == "" and ahead == "0"


__all__ = ["git_workspace_is_clean_unpublished"]
