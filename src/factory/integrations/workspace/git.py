"""Git worktree based workspace provisioning.

A run's isolated working tree is a **Git worktree** of the target repository:

```
source checkout (stays on its own branch)
      ↓  git worktree add
isolated branch  factory/<task-id>/<workspace-id>
      ↓
workspace path  <workspace-root>/<workspace-id>
```

Why a worktree rather than a clone: it is cheap, it shares the object store so
no history is duplicated, and it never disturbs the source checkout — the source
repository keeps the branch it had. The factory branch is never checked out in
the source checkout.

Safety properties, all enforced below:

* the source checkout is left on its existing branch (only ``worktree add`` runs);
* no ``fetch``, ``push``, ``reset``, ``checkout`` of a branch in the source tree,
  or any history rewrite — the command surface is deliberately tiny;
* a workspace is retry-safe: preparing an existing, matching workspace is a
  no-op, and a mismatching pre-existing checkout is refused, never reused;
* two different workspaces get two different branches and paths;
* failures are normalized to :class:`~factory.domain.errors.WorkspaceProvisioningError`
  and the raw git output — which can contain a remote URL with an embedded
  credential — is discarded rather than chained into the error.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from factory.domain.errors import WorkspaceProvisioningError
from factory.domain.models import FactoryTask, Workspace
from factory.domain.ports import WorkspaceProvisioner

#: Default per-command timeout. Git worktree operations are local and cheap.
DEFAULT_TIMEOUT_SECONDS = 60.0

#: Environment variables git legitimately needs. Everything else — including
#: any credential the factory process happens to hold — is not forwarded.
_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT")


class GitWorktreeWorkspaceProvisioner(WorkspaceProvisioner):
    """Creates one isolated Git worktree per run attempt."""

    def __init__(
        self,
        source_checkout: str,
        *,
        base_ref: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._source = Path(source_checkout).expanduser()
        self._base_ref = base_ref
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"GitWorktreeWorkspaceProvisioner(source_checkout={str(self._source)!r})"

    # -- WorkspaceProvisioner ---------------------------------------------

    def prepare(self, task: FactoryTask, workspace: Workspace) -> Workspace:
        """Materialise ``workspace`` as a worktree on its own branch.

        Raises:
            WorkspaceProvisioningError: if the source checkout is unusable, the
                workspace cannot be created, or a pre-existing workspace does not
                match the requested branch.
        """
        del task  # The workspace already carries everything needed to prepare it.
        self._require_source_checkout(workspace.workspace_id)

        target = Path(workspace.path).expanduser()
        if target.exists():
            self._confirm_existing(target, workspace)
            return workspace

        target.parent.mkdir(parents=True, exist_ok=True)
        self._create_worktree(target, workspace)
        # Confirm the result rather than trusting that ``worktree add`` succeeded
        # in the shape we asked for: the branch must be exactly the workspace's.
        self._confirm_existing(target, workspace)
        return workspace

    # -- internals ---------------------------------------------------------

    def _require_source_checkout(self, workspace_id: str) -> None:
        """The injected source checkout must exist and be a Git repository.

        Checked before any command runs, so a bad configuration fails with a
        clear factory error instead of a git usage message.
        """
        if not (self._source / ".git").exists():
            raise WorkspaceProvisioningError(workspace_id)

    def _create_worktree(self, target: Path, workspace: Workspace) -> None:
        branch = workspace.branch
        if branch.startswith("-") or "/" not in branch:
            # Defense in depth: a malformed branch must never reach git as an
            # option, and the factory always names branches as factory/<...>.
            raise WorkspaceProvisioningError(workspace.workspace_id)

        if self._branch_exists(branch, workspace.workspace_id):
            # A previous, partial attempt already created the branch but not the
            # worktree. Attaching to the existing branch is the retry-safe path;
            # the branch is reused, never deleted or recreated.
            args = ["worktree", "add", target.as_posix(), branch]
        else:
            args = [
                "worktree",
                "add",
                "-b",
                branch,
                target.as_posix(),
                self._resolved_base(workspace.workspace_id),
            ]

        self._run(args, cwd=self._source, workspace_id=workspace.workspace_id)

    def _resolved_base(self, workspace_id: str) -> str:
        """Return the commit-ish a new factory branch starts from.

        Defaults to the source checkout's current ``HEAD`` — which is *not*
        changed, and which is the only ref the factory reads. An explicit
        ``base_ref`` can be injected by the application layer.
        """
        if self._base_ref is not None:
            if self._base_ref.startswith("-"):
                raise WorkspaceProvisioningError(workspace_id)
            return self._base_ref
        return "HEAD"

    def _branch_exists(self, branch: str, workspace_id: str) -> bool:
        code = self._run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self._source,
            workspace_id=workspace_id,
            allow_failure=True,
        )
        return code == 0

    def _confirm_existing(self, target: Path, workspace: Workspace) -> None:
        """Verify an existing workspace is the one we asked for.

        Reuse is only legal when the directory is a worktree on exactly the
        expected branch. Anything else — a stale checkout, a plain directory, a
        different branch — is refused rather than silently reused, because an
        agent must never run in a workspace another attempt owns.
        """
        if not (target / ".git").exists():
            raise WorkspaceProvisioningError(workspace.workspace_id)
        actual = self._current_branch(target, workspace.workspace_id)
        if actual != workspace.branch:
            raise WorkspaceProvisioningError(workspace.workspace_id)

    def _current_branch(self, target: Path, workspace_id: str) -> str | None:
        result = self._run_capture(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=target,
            workspace_id=workspace_id,
        )
        if result is None:
            return None
        return result.strip() or None

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path,
        workspace_id: str,
        allow_failure: bool = False,
    ) -> int:
        try:
            completed = subprocess.run(  # noqa: S603 - argv form, shell is never used
                ["git", *args],
                cwd=str(cwd),
                env=self._env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # Never chain: the OS error or timeout text could carry the command
            # line (and therefore a path) and is not useful to a caller anyway.
            raise WorkspaceProvisioningError(workspace_id) from None
        if completed.returncode != 0 and not allow_failure:
            # stderr is deliberately dropped. Git echoes the failing command and
            # can include a remote URL with an embedded token in it; only the
            # fact of failure crosses this boundary.
            raise WorkspaceProvisioningError(workspace_id)
        return completed.returncode

    def _run_capture(self, args: list[str], *, cwd: Path, workspace_id: str) -> str | None:
        try:
            completed = subprocess.run(  # noqa: S603 - argv form, shell is never used
                ["git", *args],
                cwd=str(cwd),
                env=self._env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise WorkspaceProvisioningError(workspace_id) from None
        if completed.returncode != 0:
            return None
        return completed.stdout.decode("utf-8", errors="replace")

    @staticmethod
    def _env() -> dict[str, str]:
        """Minimal, secret-free environment for git subprocesses."""
        env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        # Never prompt for credentials, and never let a helper cache one.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        return env


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "GitWorktreeWorkspaceProvisioner"]
