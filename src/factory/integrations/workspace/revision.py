"""Concrete Git implementation of the ``WorkspaceRevisionInspector`` port.

A workspace revision identity is the Git *tree* object id of the complete
publishable state. Computing it never touches the workspace's real index and never
creates a commit, so binding a revision during validation has no side effect.

```
temporary index file      GIT_INDEX_FILE=<temp>
   ↓  git read-tree HEAD           (seed from the committed tree)
   ↓  git add -A                   (tracked changes, additions, deletions,
   ↓                                untracked files, modes, symlinks)
   ↓  git write-tree               (the fingerprint: a tree object id)
discard the temporary index
```

Why a Git-native tree rather than hashing files by hand: it accounts for exactly
what a later ``git add -A && git commit`` would publish — including deletions and
git's own representation of modes and symlinks — while excluding ignored files,
without the factory reimplementing git's rules.

Safety properties, all enforced below:

* **argv only, no shell.** Commands are lists; ``shell=False`` is never overridden.
* **Bounded.** Every command has a timeout; exceeding it is a sanitized failure.
* **Side-effect free.** The real index is never used as ``GIT_INDEX_FILE``, so
  binding a revision does not stage anything, and no commit is ever created.
* **Sanitized errors.** Raw stdout/stderr are discarded; the underlying process
  exception is never chained as ``__cause__``/``__context__``.
* **No credential.** Global/system git config is disabled and prompting is off,
  so inspection can never authenticate to a remote.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import tempfile
from pathlib import Path

from factory.domain.errors import WorkspaceRevisionError
from factory.domain.models import Workspace
from factory.domain.ports import WorkspaceRevisionInspector

#: Default per-command timeout. Inspecting a workspace is purely local.
DEFAULT_TIMEOUT_SECONDS = 60.0

#: Environment variables git legitimately needs. Everything else — including any
#: credential the factory process happens to hold — is not forwarded.
_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT")


class GitWorkspaceRevisionInspector(WorkspaceRevisionInspector):
    """Computes a workspace's publishable state as a Git tree object id."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    def fingerprint(self, workspace: Workspace) -> str:
        """Return the tree object id of the workspace's publishable state.

        Raises:
            WorkspaceRevisionError: if the workspace is not a Git checkout or git
                fails. The message names the workspace only.
        """
        root = Path(workspace.path).expanduser()
        if not root.is_dir() or not (root / ".git").exists():
            raise WorkspaceRevisionError(workspace.workspace_id)

        index = _TemporaryIndex()
        try:
            env = self._env({"GIT_INDEX_FILE": str(index.path)})
            # Seed the temporary index from the committed tree, then stage the
            # working tree into it. ``-A`` covers modifications, additions,
            # deletions, untracked files, modes and symlinks; ignored files are
            # excluded, exactly as a later factory commit would be.
            self._run(["read-tree", "HEAD"], root, workspace, env)
            self._run(["add", "-A"], root, workspace, env)
            output = self._capture(["write-tree"], root, workspace, env)
        finally:
            index.cleanup()

        if not output:
            raise WorkspaceRevisionError(workspace.workspace_id)
        return output

    def _run(
        self,
        args: list[str],
        cwd: Path,
        workspace: Workspace,
        env: dict[str, str],
    ) -> None:
        try:
            completed = subprocess.run(  # noqa: S603 - argv form, shell is never used
                ["git", *args],
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # The OS error or timeout text could carry the command line, and git
            # output can carry a path or remote URL, so it is discarded rather than
            # chained.
            raise WorkspaceRevisionError(workspace.workspace_id) from None
        if completed.returncode != 0:
            raise WorkspaceRevisionError(workspace.workspace_id)

    def _capture(
        self,
        args: list[str],
        cwd: Path,
        workspace: Workspace,
        env: dict[str, str],
    ) -> str:
        try:
            completed = subprocess.run(  # noqa: S603 - argv form, shell is never used
                ["git", *args],
                cwd=str(cwd),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise WorkspaceRevisionError(workspace.workspace_id) from None
        if completed.returncode != 0:
            raise WorkspaceRevisionError(workspace.workspace_id)
        return completed.stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _env(extra: dict[str, str]) -> dict[str, str]:
        """Minimal, credential-free environment for git subprocesses."""
        env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        env.update(extra)
        return env


class _TemporaryIndex:
    """A private, empty index file for one fingerprint computation."""

    def __init__(self) -> None:
        handle, name = tempfile.mkstemp(prefix="factory-index-")
        os.close(handle)
        self.path = Path(name)
        # git creates the index itself; an existing empty file is fine, and
        # removing it lets git write with its expected permissions.
        self.path.unlink()

    def cleanup(self) -> None:
        # A leftover empty temp file is not worth failing validation over.
        with contextlib.suppress(OSError):
            self.path.unlink()


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "GitWorkspaceRevisionInspector"]
