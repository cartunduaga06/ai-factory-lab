"""Concrete Git implementation of the :class:`~factory.domain.ports.WorkspacePublisher`.

Publishing is the most dangerous operation the factory performs: it is the first
time the factory writes to a remote. Every safety property is enforced here, so
neither the domain nor orchestration ever runs git.

```
Workspace (isolated worktree, branch factory/<task>/<workspace>)
      ↓  git status / commit          (inside Workspace.path only)
   deterministic factory commit        "factory: implement task <task-id>"
      ↓  git push <remote> refs/heads/<branch>:refs/heads/<branch>
   remote branch with the SAME name
```

Safety properties, all enforced below:

* **argv only, no shell.** Commands are lists; ``shell=False`` is never
  overridden. Nothing is interpolated into a command string.
* **Bounded.** Every command has a timeout; exceeding it is a sanitized failure.
* **Minimal environment.** Only a small allowlist of variables is forwarded, plus
  the write credential for the authenticated push and nothing else. Quality-gate
  subprocesses never receive it.
* **Sanitized errors.** Raw stdout/stderr are discarded. Git echoes failing
  commands and remote URLs (which can embed a token), so the underlying exception
  is never chained as ``__cause__``/``__context__``.
* **Only the workspace's own branch is pushed.** The destination ref is validated
  explicitly, ``main``/``master``/``HEAD`` are refused, and no force option is
  ever used, so history is never rewritten.
* **Hook isolation.** Factory-controlled commit and push run with
  ``core.hooksPath=/dev/null`` via a command-line ``-c`` override (never
  persisted), so a repository hook cannot execute with the write credential in
  the environment.
* **No credential in a URL or argv.** The remote URL is inspected first; a
  userinfo-bearing URL is refused. HTTPS authentication uses a temporary
  ``GIT_ASKPASS`` helper that contains no credential and reads it from a process
  environment variable; the helper is removed after use.
* **No empty commit.** A branch with no publishable diff and no previous factory
  commit is refused rather than committed. A retry reuses the existing factory
  commit instead of creating a duplicate.
"""

from __future__ import annotations

import contextlib
import os
import stat
import subprocess
import tempfile
from pathlib import Path

from factory.domain.errors import (
    PublicationError,
    RevisionNotPublishableError,
    UnsafeRemoteError,
)
from factory.domain.models import AgentRun, FactoryTask, PublishedRevision, Workspace
from factory.domain.ports import WorkspacePublisher

#: Default per-command timeout. Publishing is a local commit plus a remote push.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Branches the factory must never publish to.
PROTECTED_BRANCHES = frozenset({"main", "master"})

#: Environment variables git legitimately needs. Everything else — including any
#: credential the factory process happens to hold — is not forwarded.
_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT")

#: Environment variable names the askpass helper reads. The helper script itself
#: contains no credential; these are set only for the authenticated push.
_USERNAME_ENV = "GIT_FACTORY_USERNAME"
_PASSWORD_ENV = "GIT_FACTORY_PASSWORD"

#: Askpass helper. Contains no credential: it echoes the values of two process
#: environment variables, so the token is never written to a file, a URL or argv.
_ASKPASS_SCRIPT = f"""#!/bin/sh
case "$1" in
  *[Uu]sername*) printf '%s\\n' "${{{_USERNAME_ENV}:-x-access-token}}" ;;
  *) printf '%s\\n' "${{{_PASSWORD_ENV}}}" ;;
esac
"""


def commit_message_for(task: FactoryTask) -> str:
    """Return the deterministic, factory-controlled commit message for ``task``.

    Built from the factory's own task id only. Untrusted provider text, an agent
    summary or an error message is never copied into a commit.
    """
    return f"factory: implement task {task.task_id}"


class GitWorkspacePublisher(WorkspacePublisher):
    """Commits a validated workspace and pushes its isolated branch."""

    def __init__(
        self,
        *,
        remote: str = "origin",
        write_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._remote = remote
        self._write_token = write_token
        self._timeout = timeout

    def __repr__(self) -> str:
        # The write token is never rendered.
        return f"GitWorkspacePublisher(remote={self._remote!r})"

    # -- WorkspacePublisher ------------------------------------------------

    def publish(self, task: FactoryTask, run: AgentRun) -> PublishedRevision:
        """Commit the run's workspace changes (if any) and push its branch.

        Raises:
            PublicationError: if the workspace is missing, is not the expected
                isolated worktree, has nothing to publish, or cannot be pushed.
                All failures are sanitized.
        """
        workspace = run.workspace
        if workspace is None:
            raise PublicationError(f"run {run.run_id} has no workspace to publish")
        self._require_isolated_workspace(task, workspace)

        commit_sha = self._commit_if_needed(task, workspace)
        self._push(workspace)
        return PublishedRevision(commit_sha=commit_sha, branch=workspace.branch)

    # -- validation --------------------------------------------------------

    def _require_isolated_workspace(self, task: FactoryTask, workspace: Workspace) -> None:
        """Refuse anything that is not the run's own isolated worktree.

        Verified before any write: the directory exists, it is a worktree, the
        checked-out branch is exactly the workspace's branch, that branch is not a
        protected default branch, and the workspace belongs to the task's target
        repository. A mismatch is refused rather than committed.
        """
        path = Path(workspace.path).expanduser()
        if not path.is_dir() or not (path / ".git").exists():
            raise PublicationError(f"workspace {workspace.workspace_id} is not a Git checkout")

        branch = self._require_branch(workspace)
        if branch in PROTECTED_BRANCHES:
            raise PublicationError(f"workspace {workspace.workspace_id} is on a protected branch")
        if workspace.repository_slug != task.target_repository:
            raise PublicationError(
                f"workspace {workspace.workspace_id} does not match the task repository"
            )

    def _require_branch(self, workspace: Workspace) -> str:
        """Return the workspace's actual branch, or raise if it is not expected."""
        branch = workspace.branch
        if branch in PROTECTED_BRANCHES or branch.startswith("-") or "/" not in branch:
            # Defense in depth: a malformed or protected branch must never reach
            # git as an option or as a push destination.
            raise PublicationError(f"workspace {workspace.workspace_id} has an unsafe branch")
        actual = self._capture(["rev-parse", "--abbrev-ref", "HEAD"], workspace)
        if actual is None or actual.strip() != branch:
            raise PublicationError(
                f"workspace {workspace.workspace_id} is not on its expected branch"
            )
        return branch

    # -- commit ------------------------------------------------------------

    def _commit_if_needed(self, task: FactoryTask, workspace: Workspace) -> str:
        """Create the factory commit, or reuse a previous one, and return its sha.

        * working tree has changes → commit them;
        * clean tree with a prior factory commit → reuse that commit (retry-safe);
        * clean tree with no factory commit → refuse; there is nothing to publish.
        """
        if self._has_changes(workspace):
            self._commit(task, workspace)

        sha = self._capture(["rev-parse", "HEAD"], workspace)
        if sha is None:
            raise PublicationError(f"workspace {workspace.workspace_id} has no HEAD")
        sha = sha.strip()

        subject = self._capture(["log", "-1", "--format=%s"], workspace)
        if subject is None or subject.strip() != commit_message_for(task):
            # Nothing was committed and HEAD is not a factory commit: publishing
            # would produce an empty, meaningless commit (or an empty PR).
            raise RevisionNotPublishableError(workspace.workspace_id)
        return sha

    def _has_changes(self, workspace: Workspace) -> bool:
        status = self._capture(["status", "--porcelain"], workspace)
        return bool(status and status.strip())

    def _commit(self, task: FactoryTask, workspace: Workspace) -> None:
        self._run(["add", "-A"], workspace)
        # A deterministic factory identity, passed inline (never persisted), so a
        # commit does not depend on the machine's git configuration — which is
        # disabled for these commands. Hooks are disabled so a repository hook
        # cannot run with the write credential in the environment.
        self._run(
            [
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "user.name=ai-factory-lab",
                "-c",
                "user.email=factory@localhost",
                "commit",
                "-m",
                commit_message_for(task),
            ],
            workspace,
        )

    # -- push --------------------------------------------------------------

    def _push(self, workspace: Workspace) -> None:
        """Push exactly ``workspace.branch`` to the same remote branch name.

        The destination ref is explicit and non-force, so ``main``/``master``,
        ``HEAD`` and history rewrites are impossible. HTTPS authentication uses a
        temporary, credential-free askpass helper, created and removed around the
        single push.
        """
        branch = workspace.branch
        if branch in PROTECTED_BRANCHES or branch.startswith("+"):
            raise PublicationError(f"workspace {workspace.workspace_id} has an unsafe branch")

        url = self._remote_url(workspace)
        refspec = f"refs/heads/{branch}:refs/heads/{branch}"

        if _is_https(url):
            if self._write_token is None:
                # Never fall back to the read-only intake credential implicitly.
                raise PublicationError(
                    f"workspace {workspace.workspace_id} requires a write credential"
                )
            self._push_with_token(workspace, refspec)
            return

        self._run(["-c", "core.hooksPath=/dev/null", "push", self._remote, refspec], workspace)

    def _push_with_token(self, workspace: Workspace, refspec: str) -> None:
        askpass = _AskpassHelper()
        try:
            env = self._env(
                {
                    "GIT_ASKPASS": str(askpass.path),
                    _USERNAME_ENV: "x-access-token",
                    _PASSWORD_ENV: self._write_token or "",
                }
            )
            self._run(
                # Credential helpers are cleared and global/system config disabled
                # for this operation, so nothing caches or echoes the credential.
                [
                    "-c",
                    "core.hooksPath=/dev/null",
                    "-c",
                    "credential.helper=",
                    "push",
                    self._remote,
                    refspec,
                ],
                workspace,
                env=env,
            )
        finally:
            askpass.cleanup()

    # -- git plumbing ------------------------------------------------------

    def _remote_url(self, workspace: Workspace) -> str:
        """Return the configured remote URL, refusing one with embedded userinfo.

        The factory never authenticates through a credential-bearing remote, so a
        URL such as ``https://token@host/...`` is refused before any push. The URL
        is never surfaced in the error.
        """
        url = self._capture(["config", "--get", f"remote.{self._remote}.url"], workspace)
        if url is None or not url.strip():
            raise PublicationError(f"workspace {workspace.workspace_id} has no remote configured")
        url = url.strip()
        if _has_embedded_credentials(url):
            raise UnsafeRemoteError(workspace.workspace_id)
        return url

    def _capture(self, args: list[str], workspace: Workspace) -> str | None:
        result = self._execute(args, workspace, env=self._env())
        if result.returncode != 0:
            return None
        return result.stdout.decode("utf-8", errors="replace")

    def _run(
        self,
        args: list[str],
        workspace: Workspace,
        *,
        env: dict[str, str] | None = None,
    ) -> None:
        result = self._execute(args, workspace, env=env if env is not None else self._env())
        if result.returncode != 0:
            raise PublicationError(f"workspace {workspace.workspace_id} could not be published")

    def _execute(
        self, args: list[str], workspace: Workspace, *, env: dict[str, str]
    ) -> subprocess.CompletedProcess[bytes]:
        failure: PublicationError | None = None
        try:
            return subprocess.run(  # noqa: S603 - argv form, shell is never used
                ["git", *args],
                cwd=workspace.path,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            # The OS error or timeout text could carry the command line, and git
            # output can carry a remote URL with a token, so it is discarded rather
            # than inspected.
            failure = PublicationError(f"workspace {workspace.workspace_id} could not be published")
        # Raised outside the ``except`` block so the discarded exception is not
        # retained as ``__context__`` on the error.
        assert failure is not None
        raise failure

    @staticmethod
    def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
        """Minimal, secret-free environment for git subprocesses."""
        env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        if extra is not None:
            env.update(extra)
        return env


def _is_https(url: str) -> bool:
    lowered = url.lower()
    return lowered.startswith("https://") or lowered.startswith("http://")


def _has_embedded_credentials(url: str) -> bool:
    """Whether ``url`` carries userinfo (``scheme://user:pass@host``)."""
    scheme, sep, remainder = url.partition("://")
    if not sep:
        return False
    authority = remainder.split("/", 1)[0]
    return "@" in authority


class _AskpassHelper:
    """A temporary ``GIT_ASKPASS`` script that holds no credential."""

    def __init__(self) -> None:
        handle, name = tempfile.mkstemp(prefix="factory-askpass-", suffix=".sh")
        os.close(handle)
        self.path = Path(name)
        self.path.write_text(_ASKPASS_SCRIPT, encoding="utf-8")
        self.path.chmod(stat.S_IRWXU)

    def cleanup(self) -> None:
        # A leftover temp file is not worth failing publication over; it never
        # contained a credential.
        with contextlib.suppress(OSError):
            self.path.unlink()


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "GitWorkspacePublisher",
    "PROTECTED_BRANCHES",
    "commit_message_for",
]
