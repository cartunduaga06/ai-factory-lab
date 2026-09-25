"""Concrete Git implementation of the :class:`~factory.domain.ports.WorkspacePublisher`.

Publishing is the most dangerous operation the factory performs: it is the first
time the factory writes to a remote. Every safety property is enforced here, so
neither the domain nor orchestration ever runs git.

```
Workspace (isolated worktree, branch factory/<task>/<workspace>)
      ↓  git status / commit          (inside Workspace.path only)
   deterministic factory commit C      "factory: implement task <task-id>"
      ↓  verify tree(C) == run.validated_revision
      ↓  git push <remote> <C>:refs/heads/<branch>
   remote branch with the SAME name, pointing at the verified commit
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
* **Only the workspace's own branch is pushed, at an immutable revision.** The
  destination ref is validated explicitly, ``main``/``master``/``HEAD`` are
  refused, and no force option is ever used, so history is never rewritten. The
  *source* is the verified commit SHA, never the mutable local branch, so a branch
  moved between verification and the push cannot change what is sent.
* **Hook isolation.** Factory-controlled commit and push run with
  ``core.hooksPath=/dev/null`` via a command-line ``-c`` override (never
  persisted), so a repository hook cannot execute with the write credential in
  the environment.
* **The effective push destination is pinned.** The remote *name* is never
  trusted as evidence of where a push lands: a completed agent can edit
  ``remote.<name>.url``, add ``remote.<name>.pushurl``, or install
  ``url.<base>.insteadOf`` / ``url.<base>.pushInsteadOf`` rewrites. The publisher
  therefore asks Git for its own resolution — ``git remote get-url --push --all``
  — and validates what Git will actually push to. A network publication must
  resolve to exactly one destination, and an authenticated push is then performed
  by remote name, which Git resolves with the very same algorithm (an explicit
  URL argument is *not* used, because a rewrite can retarget it).
* **The destination is pinned to an allowed host and the task's repository.**
  An HTTPS network push is parsed with URL parsing — never substring matching —
  and accepted only when the host equals the configured allowed Git host, the
  path is exactly ``/<owner>/<repo>`` or ``/<owner>/<repo>.git``, and there is no
  userinfo, port, query or fragment. HTTPS alone is not sufficient.
* **Network publication is HTTPS-only.** ``ssh://``, ``git://``, plaintext
  ``http://`` and scp-style ``git@host:owner/repo.git`` remotes are refused:
  they could authenticate with ambient machine credentials (``~/.ssh``, an SSH
  agent) instead of the dedicated write credential. Local filesystem remotes are
  unaffected and still need no credential.
* **No credential in a URL or argv.** A userinfo-bearing destination is refused,
  and HTTPS authentication uses a temporary ``GIT_ASKPASS`` helper that contains
  no credential and reads it from a process environment variable; the helper is
  removed after use. The token never reaches a URL or argv.
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
from urllib.parse import urlsplit

from factory.domain.errors import (
    PublicationError,
    RevisionNotPublishableError,
    UnsafeRemoteError,
    ValidatedRevisionMismatchError,
    ValidatedRevisionMissingError,
)
from factory.domain.models import AgentRun, FactoryTask, PublishedRevision, Workspace
from factory.domain.ports import WorkspacePublisher, WorkspaceRevisionInspector
from factory.integrations.workspace.revision import GitWorkspaceRevisionInspector

#: Default per-command timeout. Publishing is a local commit plus a remote push.
DEFAULT_TIMEOUT_SECONDS = 120.0

#: Branches the factory must never publish to.
PROTECTED_BRANCHES = frozenset({"main", "master"})

#: The Git host an authenticated HTTPS push may target. Injectable so a future
#: deployment (a GitHub Enterprise host, say) does not require editing this class.
DEFAULT_GIT_HOST = "github.com"

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
        allowed_git_host: str = DEFAULT_GIT_HOST,
        revision_inspector: WorkspaceRevisionInspector | None = None,
    ) -> None:
        self._remote = remote
        self._write_token = write_token
        self._timeout = timeout
        self._allowed_git_host = allowed_git_host.strip().lower().rstrip(".")
        self._revision_inspector = revision_inspector or GitWorkspaceRevisionInspector(
            timeout=timeout
        )

    def __repr__(self) -> str:
        # The write token is never rendered.
        return f"GitWorkspacePublisher(remote={self._remote!r}, host={self._allowed_git_host!r})"

    # -- WorkspacePublisher ------------------------------------------------

    def publish(self, task: FactoryTask, run: AgentRun) -> PublishedRevision:
        """Commit the run's workspace changes (if any) and push its branch.

        The workspace is checked against the run's bound revision *before* any
        real ``git add``, commit, credential exposure or push, and the resulting
        commit's tree is checked against it again before the push — defense in
        depth that only the revision which passed the quality gates is published.

        Raises:
            ValidatedRevisionMissingError: if the run carries no bound revision.
            ValidatedRevisionMismatchError: if the workspace (or the commit's
                tree) does not match that revision.
            PublicationError: if the workspace is missing, is not the expected
                isolated worktree, has nothing to publish, or cannot be pushed.
                All failures are sanitized.
        """
        workspace = run.workspace
        if workspace is None:
            raise PublicationError(f"run {run.run_id} has no workspace to publish")
        self._require_isolated_workspace(task, workspace)

        validated = _validated_revision(run)
        if not validated:
            raise ValidatedRevisionMissingError(task.task_id, run.run_id)
        if self._current_revision(workspace) != validated:
            raise ValidatedRevisionMismatchError(workspace.workspace_id)

        commit_sha = self._commit_if_needed(task, workspace)
        if self._tree_of(commit_sha, workspace) != validated:
            # The commit does not contain exactly the tree that passed validation.
            raise ValidatedRevisionMismatchError(workspace.workspace_id)
        self._push(task, workspace, commit_sha)
        return PublishedRevision(commit_sha=commit_sha, branch=workspace.branch)

    # -- revision identity -------------------------------------------------

    def _current_revision(self, workspace: Workspace) -> str | None:
        """Return the workspace's current publishable revision, or ``None``.

        Never raises: an inspection failure is a refusal, not a crash, and the
        sanitized inspector error is discarded rather than chained.
        """
        try:
            return self._revision_inspector.fingerprint(workspace)
        except Exception:  # noqa: BLE001 - a defective inspector must not leak
            return None

    def _tree_of(self, commit_sha: str, workspace: Workspace) -> str | None:
        """Return the tree object id of ``commit_sha``, or ``None``."""
        tree = self._capture(["rev-parse", f"{commit_sha}^{{tree}}"], workspace)
        return tree.strip() if tree else None

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

    def _push(self, task: FactoryTask, workspace: Workspace, commit_sha: str) -> None:
        """Push the verified immutable commit to exactly ``workspace.branch``.

        The source ref is the commit SHA that was just verified against
        ``run.validated_revision`` — never the mutable local branch. The branch
        ref could be moved by another process between verification and the push
        (a TOCTOU window), so ``refs/heads/<branch>`` as a *source* would risk
        sending a commit that never passed validation. Using the SHA makes the
        published commit immutable and independent of local branch movement.

        The destination ref is explicit and non-force, so ``main``/``master``,
        ``HEAD`` and history rewrites are impossible.

        The *effective* push destination — what Git itself resolves, including
        ``remote.<name>.pushurl`` and ``url.*.insteadOf``/``pushInsteadOf``
        rewrites — is inspected before anything else happens:

        * a single local filesystem destination is pushed without a credential;
        * a single network destination must be an allowed HTTPS GitHub target for
          the task's repository, and is then pushed with the write credential;
        * anything else (another host, another owner/repository, a plaintext or
          SSH/scp scheme, userinfo, multiple destinations) is refused with a
          sanitized :class:`UnsafeRemoteError` before the credential is exposed.

        The push is issued *by remote name*. Git resolves a remote name with the
        same algorithm ``git remote get-url --push --all`` used, so the validated
        destination is the destination pushed to. An explicit URL argument is
        deliberately avoided: a rewrite rule can retarget one.
        """
        branch = workspace.branch
        if branch in PROTECTED_BRANCHES or branch.startswith("+"):
            raise PublicationError(f"workspace {workspace.workspace_id} has an unsafe branch")

        refspec = f"{commit_sha}:refs/heads/{branch}"
        destinations = self._effective_push_urls(workspace)
        network = [url for url in destinations if not _is_local_path(url)]

        if not network:
            # Local disposable remotes need no credential and keep working.
            self._run(["-c", "core.hooksPath=/dev/null", "push", self._remote, refspec], workspace)
            return

        if len(destinations) != 1:
            # Never fan a branch or a credential out to more than one destination.
            raise UnsafeRemoteError(workspace.workspace_id)

        self._validate_network_destination(task, workspace, destinations[0])

        if self._write_token is None:
            # Never fall back to the read-only intake credential implicitly.
            raise PublicationError(
                f"workspace {workspace.workspace_id} requires a write credential"
            )
        self._push_with_token(workspace, refspec)

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
                # The remote name is resolved with the same algorithm that was
                # validated, so the destination cannot drift after validation.
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

    def _effective_push_urls(self, workspace: Workspace) -> list[str]:
        """Return the effective push URLs Git will actually use for the remote.

        Uses Git's own resolution (``git remote get-url --push --all``) rather
        than reading ``remote.<name>.url`` directly: a ``pushurl`` or a
        ``url.*.insteadOf``/``pushInsteadOf`` rewrite can retarget a push without
        changing the configured URL. The returned URLs are never logged or
        surfaced in an error.
        """
        output = self._capture(["remote", "get-url", "--push", "--all", self._remote], workspace)
        if output is None:
            raise PublicationError(f"workspace {workspace.workspace_id} has no remote configured")
        urls = [line.strip() for line in output.splitlines() if line.strip()]
        if not urls:
            raise PublicationError(f"workspace {workspace.workspace_id} has no remote configured")
        return urls

    def _validate_network_destination(
        self, task: FactoryTask, workspace: Workspace, url: str
    ) -> None:
        """Refuse a network destination that is not the allowed HTTPS target.

        The URL is parsed structurally — never by substring matching — and
        accepted only when the scheme is HTTPS, the host equals the configured
        allowed Git host, and the path is exactly ``/<owner>/<repo>`` (an
        optional trailing ``.git`` aside) for the task's target repository. Any
        userinfo, explicit port, query or fragment is refused. The URL is never
        echoed in the error.
        """
        parts = urlsplit(url) if _splittable(url) else None
        if parts is None:
            raise UnsafeRemoteError(workspace.workspace_id)
        if parts.scheme.lower() != "https":
            # http://, ssh://, git:// and scp-style remotes may authenticate with
            # ambient machine credentials; MVP 0.1 publishes over HTTPS only.
            raise UnsafeRemoteError(workspace.workspace_id)
        if "@" in parts.netloc or parts.username is not None or parts.password is not None:
            raise UnsafeRemoteError(workspace.workspace_id)
        try:
            port = parts.port
        except ValueError:
            raise UnsafeRemoteError(workspace.workspace_id) from None
        if port is not None:
            raise UnsafeRemoteError(workspace.workspace_id)
        host = (parts.hostname or "").lower().rstrip(".")
        if host != self._allowed_git_host:
            raise UnsafeRemoteError(workspace.workspace_id)
        if parts.query or parts.fragment:
            raise UnsafeRemoteError(workspace.workspace_id)
        expected = "/" + task.target_repository
        if parts.path not in (expected, expected + ".git"):
            raise UnsafeRemoteError(workspace.workspace_id)

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


def _validated_revision(run: AgentRun) -> str:
    """The run's bound validated revision, or an empty string if it has none."""
    revision = run.validated_revision
    return revision.strip() if revision else ""


def _splittable(url: str) -> bool:
    """Whether ``urlsplit`` can parse ``url`` without raising.

    A malformed authority (for example an unterminated IPv6 literal) makes
    ``urlsplit`` raise; the caller treats that as an unsafe destination rather
    than letting the exception escape.
    """
    try:
        urlsplit(url)
    except ValueError:
        return False
    return True


def _is_local_path(url: str) -> bool:
    """Whether ``url`` is a local path rather than a network destination.

    A local path is what a disposable test remote looks like: a filesystem path
    (absolute, or ``./``/``../``-relative) or a ``file://`` URL. These need no
    credential and are not subject to destination pinning.
    """
    if url.lower().startswith("file://"):
        return True
    if url.startswith(("/", "./", "../", "~")):
        return True
    # A Windows drive path (C:\...) or a path without a URI scheme.
    if len(url) >= 2 and url[1] == ":" and url[0].isalpha():
        return True
    scheme, sep, _ = url.partition("://")
    if sep:
        return False
    return ":" not in scheme


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
    "DEFAULT_GIT_HOST",
    "DEFAULT_TIMEOUT_SECONDS",
    "GitWorkspacePublisher",
    "PROTECTED_BRANCHES",
    "commit_message_for",
]
