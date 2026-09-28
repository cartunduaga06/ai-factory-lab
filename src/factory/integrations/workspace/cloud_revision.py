"""Materialise an OpenHands Cloud revision into a local validation workspace."""

from __future__ import annotations

import contextlib
import os
import re
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from urllib.parse import urlparse

from factory.domain.models import RemoteRevision, Workspace

DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_REMOTE = "origin"
PROTECTED_BRANCHES = frozenset({"main", "master", "HEAD"})
_ENV_ALLOWLIST = ("PATH", "HOME", "TMPDIR", "TMP", "TEMP", "LANG", "LC_ALL", "SYSTEMROOT")
_SHA_PATTERN = re.compile(r"^[0-9a-fA-F]{7,64}$")
_TOKEN_ENV = "AI_FACTORY_GIT_TOKEN"
_USER_ENV = "AI_FACTORY_GIT_USERNAME"
_ASKPASS_SCRIPT = """#!/bin/sh
case "$1" in
  *[Uu]sername*) printf '%s\n' "${AI_FACTORY_GIT_USERNAME:-x-access-token}" ;;
  *) printf '%s\n' "$AI_FACTORY_GIT_TOKEN" ;;
esac
"""


class CloudRevisionError(RuntimeError):
    """A Cloud revision could not be safely materialised."""

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"workspace {workspace_id} cloud revision could not be materialised")
        self.workspace_id = workspace_id


class CloudRevisionProvider(ABC):
    """Turns a remote revision into a local, validatable workspace checkout."""

    @abstractmethod
    def materialize(self, workspace: Workspace, revision: RemoteRevision) -> None:
        """Create or confirm workspace checked out at exactly revision."""


class _AskpassHelper:
    """Temporary credential helper containing no credential value."""

    def __init__(self) -> None:
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="ai-factory-cloud-askpass-",
            delete=False,
            encoding="utf-8",
        ) as handle:
            self.path = Path(handle.name)
            handle.write(_ASKPASS_SCRIPT)
        self.path.chmod(0o700)

    def cleanup(self) -> None:
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


class GitCloudRevisionProvider(CloudRevisionProvider):
    """Fetch a Cloud branch and materialise only its exact claimed commit."""

    def __init__(
        self,
        source_checkout: str,
        *,
        remote: str = DEFAULT_REMOTE,
        read_token: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not source_checkout.strip():
            raise ValueError("cloud revision source checkout must not be empty")
        if not remote.strip() or remote.startswith("-"):
            raise ValueError("cloud revision remote must be a plain remote name")
        self._source = Path(source_checkout).expanduser()
        self._remote = remote
        self._read_token = read_token
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

        self._require_expected_remote(workspace_id, revision.repository_slug)
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

    def _require_source(self, workspace_id: str) -> None:
        if not (self._source / ".git").exists():
            raise CloudRevisionError(workspace_id)

    def _require_expected_remote(self, workspace_id: str, repository_slug: str) -> None:
        remote_url = self._capture(
            ["remote", "get-url", self._remote],
            cwd=self._source,
            workspace_id=workspace_id,
        )
        if remote_url is None:
            raise CloudRevisionError(workspace_id)
        parsed = urlparse(remote_url)
        if parsed.scheme in {"", "file"}:
            # Disposable/local remotes are useful for deterministic tests and
            # development and carry no credential or network destination.
            return
        if self._github_slug(remote_url) != repository_slug:
            raise CloudRevisionError(workspace_id)

    @staticmethod
    def _github_slug(remote_url: str) -> str | None:
        parsed = urlparse(remote_url)
        if parsed.scheme != "https" or parsed.hostname != "github.com" or parsed.username:
            return None
        path = parsed.path.strip("/").removesuffix(".git")
        if path.count("/") != 1:
            return None
        owner, name = path.split("/", 1)
        if not owner or not name:
            return None
        return f"{owner}/{name}"

    def _fetch(self, workspace_id: str, revision: RemoteRevision) -> None:
        refspec = f"+refs/heads/{revision.branch}:refs/remotes/{self._remote}/{revision.branch}"
        args = ["-c", "credential.helper=", "fetch", "--no-tags", self._remote, refspec]
        askpass: _AskpassHelper | None = None
        try:
            env = self._env()
            if self._read_token:
                askpass = _AskpassHelper()
                env["GIT_ASKPASS"] = str(askpass.path)
                env[_USER_ENV] = "x-access-token"
                env[_TOKEN_ENV] = self._read_token
            self._run(args, cwd=self._source, workspace_id=workspace_id, env=env)
        finally:
            if askpass is not None:
                askpass.cleanup()

        remote_ref = f"refs/remotes/{self._remote}/{revision.branch}"
        fetched = self._capture(
            ["rev-parse", "--verify", remote_ref],
            cwd=self._source,
            workspace_id=workspace_id,
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
                raise CloudRevisionError(workspace_id)
            args = ["worktree", "add", target.as_posix(), branch]
        else:
            args = ["worktree", "add", "-b", branch, target.as_posix(), revision.commit_sha]
        self._run(args, cwd=self._source, workspace_id=workspace_id)

    def _reconcile(self, target: Path, revision: RemoteRevision, workspace_id: str) -> None:
        if not (target / ".git").exists():
            raise CloudRevisionError(workspace_id)
        branch = self._capture(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=target,
            workspace_id=workspace_id,
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
        return (
            self._run(
                ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
                cwd=self._source,
                workspace_id=workspace_id,
                allow_failure=True,
            )
            == 0
        )

    def _confirm(self, target: Path, revision: RemoteRevision, workspace_id: str) -> None:
        if not (target / ".git").exists():
            raise CloudRevisionError(workspace_id)
        actual_branch = self._capture(
            ["rev-parse", "--abbrev-ref", "HEAD"],
            cwd=target,
            workspace_id=workspace_id,
        )
        actual_sha = self._capture(["rev-parse", "HEAD"], cwd=target, workspace_id=workspace_id)
        if actual_branch != revision.branch or actual_sha != revision.commit_sha:
            raise CloudRevisionError(workspace_id)

    def _run(
        self,
        args: list[str],
        *,
        cwd: Path,
        workspace_id: str,
        allow_failure: bool = False,
        env: dict[str, str] | None = None,
    ) -> int:
        try:
            completed = subprocess.run(  # noqa: S603 - argv only
                ["git", *args],
                cwd=str(cwd),
                env=env or self._env(),
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
            completed = subprocess.run(  # noqa: S603 - argv only
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
    def _env(extra: dict[str, str] | None = None) -> dict[str, str]:
        env = {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        env["GIT_CONFIG_GLOBAL"] = os.devnull
        env["GIT_CONFIG_SYSTEM"] = os.devnull
        if extra:
            env.update(extra)
        return env


__all__ = [
    "DEFAULT_REMOTE",
    "DEFAULT_TIMEOUT_SECONDS",
    "PROTECTED_BRANCHES",
    "CloudRevisionError",
    "CloudRevisionProvider",
    "GitCloudRevisionProvider",
]
