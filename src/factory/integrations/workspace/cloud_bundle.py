"""Secretless Git bundle exchange with an isolated local validation worktree."""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from factory.domain.models import RemoteRevision, Workspace
from factory.integrations.workspace.cloud_revision import CloudRevisionError

_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_BRANCH = re.compile(r"factory/[A-Za-z0-9_./-]+\Z")
_MAX_BUNDLE = 200 * 1024 * 1024


class CloudBundleProvider(Protocol):
    """Local side of the Cloud transfer, with no remote Git operation."""

    def prepare_input(self, workspace: Workspace) -> tuple[bytes, str]:
        """Bundle exactly the provisioned branch and return its immutable base SHA."""
        ...

    def materialize_bundle(
        self, workspace: Workspace, revision: RemoteRevision, bundle: bytes, base_sha: str
    ) -> None:
        """Verify and import the exact bundled result into the local worktree."""
        ...


class GitCloudBundleProvider:
    """All Git commands are bounded argv calls with isolated configuration."""

    def __init__(self, source_checkout: str, *, timeout: float = 120.0) -> None:
        self._source = Path(source_checkout)
        self._timeout = timeout

    def _git_bytes(
        self, cwd: Path, workspace: Workspace, *args: str, input_data: bytes | None = None
    ) -> bytes:
        env = {
            key: os.environ[key] for key in ("PATH", "HOME", "TMPDIR", "LANG") if key in os.environ
        }
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_SYSTEM=os.devnull,
            GIT_TERMINAL_PROMPT="0",
            GIT_ASKPASS="",
            GIT_NO_REPLACE_OBJECTS="1",
        )
        try:
            result = subprocess.run(
                [
                    "git",
                    "-c",
                    "protocol.file.allow=always",
                    "-c",
                    "core.hooksPath=/dev/null",
                    *args,
                ],
                cwd=cwd,
                env=env,
                input=input_data,
                stdin=subprocess.DEVNULL if input_data is None else None,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise CloudRevisionError(workspace.workspace_id) from None
        if result.returncode:
            raise CloudRevisionError(workspace.workspace_id)
        return result.stdout

    def _git(self, cwd: Path, workspace: Workspace, *args: str) -> str:
        return self._git_bytes(cwd, workspace, *args).decode("utf-8", errors="replace").strip()

    def _check(self, workspace: Workspace, *, require_clean: bool = True) -> tuple[Path, str]:
        path = Path(workspace.path)
        if (
            path.is_symlink()
            or not (path / ".git").exists()
            or not _BRANCH.fullmatch(workspace.branch)
        ):
            raise CloudRevisionError(workspace.workspace_id)
        if self._git(path, workspace, "rev-parse", "--abbrev-ref", "HEAD") != workspace.branch:
            raise CloudRevisionError(workspace.workspace_id)
        if require_clean and self._git(
            path, workspace, "status", "--porcelain", "--untracked-files=all"
        ):
            raise CloudRevisionError(workspace.workspace_id)
        sha = self._git(path, workspace, "rev-parse", "HEAD")
        if not _SHA.fullmatch(sha):
            raise CloudRevisionError(workspace.workspace_id)
        return path, sha

    def prepare_input(self, workspace: Workspace) -> tuple[bytes, str]:
        path, base = self._check(workspace)
        with tempfile.TemporaryDirectory(prefix="factory-cloud-input-") as temporary:
            bundle = Path(temporary) / "input.bundle"
            self._git(
                path, workspace, "bundle", "create", str(bundle), f"refs/heads/{workspace.branch}"
            )
            heads = self._git(path, workspace, "bundle", "list-heads", str(bundle)).splitlines()
            if (
                heads != [f"{base} refs/heads/{workspace.branch}"]
                or self._check(workspace)[1] != base
            ):
                raise CloudRevisionError(workspace.workspace_id)
            data = bundle.read_bytes()
        if not data or len(data) > _MAX_BUNDLE:
            raise CloudRevisionError(workspace.workspace_id)
        return data, base

    def materialize_bundle(
        self, workspace: Workspace, revision: RemoteRevision, bundle: bytes, base_sha: str
    ) -> None:
        path, current = self._check(workspace, require_clean=False)
        if (
            current != base_sha
            or revision.branch != workspace.branch
            or revision.repository_slug != workspace.repository_slug
            or not _SHA.fullmatch(base_sha)
            or not _SHA.fullmatch(revision.commit_sha)
            or not bundle
            or len(bundle) > _MAX_BUNDLE
        ):
            raise CloudRevisionError(workspace.workspace_id)
        with tempfile.TemporaryDirectory(prefix="factory-cloud-result-") as temporary:
            file = Path(temporary) / "result.bundle"
            file.write_bytes(bundle)
            self._git(path, workspace, "bundle", "verify", str(file))
            heads = self._git(path, workspace, "bundle", "list-heads", str(file)).splitlines()
            if heads != [f"{revision.commit_sha} refs/heads/{workspace.branch}"]:
                raise CloudRevisionError(workspace.workspace_id)
            ref = f"refs/factory-cloud/{uuid4().hex}"
            try:
                self._git(
                    path,
                    workspace,
                    "fetch",
                    "--no-tags",
                    str(file),
                    f"refs/heads/{workspace.branch}:{ref}",
                )
                fetched = self._git(path, workspace, "rev-parse", ref)
                if fetched != revision.commit_sha:
                    raise CloudRevisionError(workspace.workspace_id)
                self._git(path, workspace, "merge-base", "--is-ancestor", base_sha, fetched)
                result_tree = self._git(path, workspace, "rev-parse", f"{fetched}^{{tree}}")
                status = self._git(
                    path, workspace, "status", "--porcelain", "--untracked-files=all"
                )
                if status:
                    # A retry may find the result already staged. Any other local
                    # changes are refused before touching the worktree.
                    self._verify_result(path, workspace, base_sha, result_tree)
                elif self._git(path, workspace, "write-tree") != result_tree:
                    patch = self._git_bytes(
                        path,
                        workspace,
                        "diff",
                        "--binary",
                        "--full-index",
                        "--no-renames",
                        base_sha,
                        ref,
                    )
                    self._git_bytes(
                        path, workspace, "apply", "--index", "--binary", "-", input_data=patch
                    )
                self._verify_result(path, workspace, base_sha, result_tree)
            finally:
                with suppress(CloudRevisionError):
                    self._git(path, workspace, "update-ref", "-d", ref)
                if self._git(path, workspace, "for-each-ref", "--format=%(refname)", ref):
                    raise CloudRevisionError(workspace.workspace_id)

    def _verify_result(
        self, path: Path, workspace: Workspace, base_sha: str, result_tree: str
    ) -> None:
        if (
            self._git(path, workspace, "rev-parse", "HEAD") != base_sha
            or self._git(path, workspace, "rev-parse", "--abbrev-ref", "HEAD") != workspace.branch
            or self._git(path, workspace, "write-tree") != result_tree
            or self._git(path, workspace, "ls-files", "--others", "--exclude-standard")
        ):
            raise CloudRevisionError(workspace.workspace_id)
        self._git(path, workspace, "diff", "--exit-code")


__all__ = ["CloudBundleProvider", "GitCloudBundleProvider"]
