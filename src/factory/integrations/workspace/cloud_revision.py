"""Materialise an OpenHands Cloud revision into a local validation workspace.

A Cloud run works inside an OpenHands Cloud sandbox, outside the factory's own
filesystem. Before the factory may publish anything it needs a deterministic,
immutable revision identity that it can *independently* validate. This module is
the seam that turns a revision a Cloud run claims it produced into a fresh local
Git worktree checked out at exactly that commit, so the existing quality gates and
revision binding can run against it.

```
Cloud sandbox (cloud repository checkout on a factory branch)
        ↓  the run reports its head commit sha
RemoteRevision(commit_sha, branch, repository_slug)   (a pointer, not a validation)
        ↓  GitCloudRevisionProvider.materialize
local source checkout ── git fetch <remote> <branch> ──► verify fetched sha == commit_sha
        ↓  git worktree add <path> <branch>  (fresh checkout at the exact commit)
local validation workspace  ← the existing gates + GitWorkspaceRevisionInspector run here
```

Safety properties, all enforced below:

* **Fail closed.** A missing/short/malformed sha, a branch that does not match the
  workspace, a fetch that resolves to a different commit (the branch moved, or was
  never pushed), or a missing local branch all raise
  :class:`CloudRevisionError` — nothing is materialised and the run is not
  publishable. The factory never invents a weaker validation path.
* **argv only, no shell.** Commands are lists; ``shell=False`` is never overridden.
* **Bounded.** Every command has a timeout; exceeding it is a sanitized failure.
* **Credential isolation.** Only a small environment allowlist is forwarded, global
  and system Git config are disabled, and prompting is off. Raw git output — which
  can carry a remote URL with an embedded token — is discarded rather than chained.
* **Isolated branch.** The worktree is created on the workspace's own factory
  branch; ``main``/``master``/``HEAD`` and option-like branches are refused.
"""

from __future__ import annotations

import os
import re
import subprocess
from abc import ABC, abstractmethod
from pathlib import Path

from factory.domain.models import RemoteRevision, Workspace

#: Default per-command timeout. Fetching plus a worktree add is bounded.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Remote name a materialisation fetches from by default.
DEFAULT_REMOTE = "origin"

#: Branches the factory never materialises onto.
PROTECTED_BRANCHES = frozenset({"main", "master", "HEAD"})

#: Environment variables git legitimately needs. Everything else — including any
#: credential the factory process happens to hold — is not forwarded.
_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT")

#: A commit sha is hex only, so it can never reach git argv as an option.
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")


class CloudRevisionError(RuntimeError):
    """A Cloud revision could not be safely materialised.

    Sanitized: never carries raw git stdout/stderr, a command line, a remote URL
    or a credential. The message names the workspace only.
    """

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"workspace {workspace_id} cloud revision could not be materialised")
        self.workspace_id = workspace_id


class CloudRevisionProvider(ABC):
    """Turns a remote revision into a local, validatable workspace checkout."""

    @abstractmethod
    def materialize(self, workspace: Workspace, revision: RemoteRevision) -> None:
        """Create or confirm ``workspace`` checked out at exactly ``revision``.

        Implementations must be **retry-safe**: materialising the same revision
        into an already-correct workspace is an identity-preserving no-op; a
        workspace or revision that does not match is refused rather than mutated.

        Raises:
            CloudRevisionError: if the revision cannot be retrieved, is unsafe, or
                does not match the workspace. Sanitized.
        """


class GitCloudRevisionProvider(CloudRevisionProvider):
    """Fetches a Cloud revision from a Git remote into a local worktree."""

    def __init__(
        self,
        source_checkout: str,
        *,
        remote: str = DEFAULT_REMOTE,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not source_checkout.strip():
            raise ValueError("cloud revision source checkout must not be empty")
        if not remote.strip() or remote.startswith("-"):
            raise ValueError("cloud revision remote must be a plain remote name")
        self._source = Path(source_checkout).expanduser()
        self._remote = remote
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"GitCloudRevisionProvider(source_checkout={str(self._source)!r})"

    def materialize(self, workspace: Workspace, revision: RemoteRevision) -> None:
        workspace_id = workspace.workspace_id
        self._require_source(workspace_id)
        if revision.repository_slug != workspace.repository_slug:
            raise CloudRevisionError(workspace_id)
        if revision.branch != workspace.branch:
            raise CloudRevisionError(workspace_id)
        if revision.branch in PROTECTED_BRANCHES or revision.branch.startswith("-"):
            raise CloudRevisionError(workspace_id)
        if "/" not in revision.branch:
            raise CloudRevisionError(workspace_id)
        if not _SHA_PATTERN.fullmatch(revision.commit_sha):
            raise CloudRevisionError(workspace_id)

        self._fetch(workspace_id, revision)
        target = Path(workspace.path).expanduser()
        if target.is_symlink():
            raise CloudRevisionError(workspace_id)
        if target.exists():
            self._reconcile(target, revision, workspace_id)
        else:
            if not target.parent.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
            self._add_worktree(target, revision, workspace_id)
        self._confirm(target, revision, workspace_id)

    # -- internals ---------------------------------------------------------

    def _require_source(self, workspace_id: str) -> None:
        if not (self._source / ".git").exists():
            raise CloudRevisionError(workspace_id)

    def _fetch(self, workspace_id: str, revision: RemoteRevision) -> None:
        """Fetch exactly the factory branch and require it to be the claimed sha.

        A branch that is not present on the remote, or that has moved on since the
        run reported its head, is refused: the factory never materialises a
        revision it cannot retrieve deterministically.
        """
        refspec = f"+refs/heads/{revision.branch}:refs/remotes/{self._remote}/{revision.branch}"
        self._run(
            ["fetch", "--no-tags", self._remote, refspec],
            cwd=self._source,
            workspace_id=workspace_id,
        )
        remote_ref = f"refs/remotes/{self._remote}/{revision.branch}"
        fetched = self._capture(
            ["rev-parse", "--verify", remote_ref], cwd=self._source, workspace_id=workspace_id
        )
        if fetched is None or fetched != revision.commit_sha:
            raise CloudRevisionError(workspace_id)

    def _add_worktree(self, target: Path, revision: RemoteRevision, workspace_id: str) -> None:
        branch = revision.branch
        if self._local_branch_exists(branch, workspace_id):
            local = self._capture(
                ["rev-parse", "--verify", f"refs/heads/{branch}"],
                cwd=self._source,
                workspace_id=workspace_id,
            )
            if local != revision.commit_sha:
                # A stale local branch points at a different commit: refuse rather
                # than reuse code that was never validated.
                raise CloudRevisionError(workspace_id)
            args = ["worktree", "add", target.as_posix(), branch]
        else:
            args = ["worktree", "add", "-b", branch, target.as_posix(), revision.commit_sha]
        self._run(args, cwd=self._source, workspace_id=workspace_id)

    def _reconcile(self, target: Path, revision: RemoteRevision, workspace_id: str) -> None:
        """Advance the run's own worktree to the fetched commit, fast-forward only.

        The dispatch-time provisioner creates the worktree at the branch base; the
        cloud run then commits on the same branch and pushes it. Descending the
        worktree to the fetched commit is a fast-forward on the *run's own isolated
        branch* — never a rewrite of anything shared. A worktree that is already at
        the commit is an identity-preserving no-op (retry-safe); a worktree that
        does not match the workspace, or cannot fast-forward to the exact commit, is
        refused rather than reset.
        """
        if not (target / ".git").exists():
            raise CloudRevisionError(workspace_id)
        branch = self._capture(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=target, workspace_id=workspace_id
        )
        if branch != revision.branch:
            raise CloudRevisionError(workspace_id)
        current = self._capture(["rev-parse", "HEAD"], cwd=target, workspace_id=workspace_id)
        if current == revision.commit_sha:
            return
        if current is None:
            raise CloudRevisionError(workspace_id)
        self._run(
            ["merge", "--ff-only", "--no-edit", revision.commit_sha],
            cwd=target,
            workspace_id=workspace_id,
        )

    def _local_branch_exists(self, branch: str, workspace_id: str) -> bool:
        code = self._run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
            cwd=self._source,
            workspace_id=workspace_id,
            allow_failure=True,
        )
        return code == 0

    def _confirm(self, target: Path, revision: RemoteRevision, workspace_id: str) -> None:
        """Verify an existing checkout is exactly the requested revision."""
        if not (target / ".git").exists():
            raise CloudRevisionError(workspace_id)
        actual_branch = self._capture(
            ["rev-parse", "--abbrev-ref", "HEAD"], cwd=target, workspace_id=workspace_id
        )
        actual_sha = self._capture(["rev-parse", "HEAD"], cwd=target, workspace_id=workspace_id)
        if actual_branch != revision.branch or actual_sha != revision.commit_sha:
            raise CloudRevisionError(workspace_id)

    # -- git plumbing ------------------------------------------------------

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
            raise CloudRevisionError(workspace_id) from None
        if completed.returncode != 0 and not allow_failure:
            raise CloudRevisionError(workspace_id)
        return completed.returncode

    def _capture(self, args: list[str], *, cwd: Path, workspace_id: str) -> str | None:
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
            raise CloudRevisionError(workspace_id) from None
        if completed.returncode != 0:
            return None
        return os.fsdecode(completed.stdout).strip() or None

    @staticmethod
    def _env() -> dict[str, str]:
        env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        return env


__all__ = [
    "DEFAULT_REMOTE",
    "DEFAULT_TIMEOUT_SECONDS",
    "PROTECTED_BRANCHES",
    "CloudRevisionError",
    "CloudRevisionProvider",
    "GitCloudRevisionProvider",
]
