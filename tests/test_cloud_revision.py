"""Tests for materialising a Cloud revision into a local validation workspace.

Every test builds disposable Git repositories under ``tmp_path``: a *remote* the
factory fetches from, a *source checkout* the factory owns, and a *workspace*
worktree. No test touches a real repository, the network or the machine's global
Git configuration.

The point of these tests is the invariant that makes Cloud execution safe: the
factory only ever validates and publishes a revision it could retrieve
deterministically and check out locally. Anything else fails closed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from factory.domain.enums import AgentBackend
from factory.domain.models import FactoryTask, RemoteRevision, TaskSource, Workspace, new_workspace
from factory.integrations.workspace.cloud_provisioner import CloudWorkspaceProvisioner
from factory.integrations.workspace.cloud_revision import (
    CloudRevisionError,
    GitCloudRevisionProvider,
)

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        **_GIT_IDENTITY,
    }


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env=_env(cwd),
    )
    return result.stdout.decode().strip()


def _bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    return remote


def _seed_remote(tmp_path: Path, remote: Path) -> None:
    """Give the remote a ``main`` commit by way of a throwaway clone."""
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-b", "main")
    (seed / "README.md").write_text("hello\n", encoding="utf-8")
    _git(seed, "add", "README.md")
    _git(seed, "commit", "-m", "initial")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "origin", "main")


def _source_checkout(tmp_path: Path, remote: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "clone", str(remote), str(source))
    return source


def _push_branch_with_commit(remote: Path, tmp_path: Path, branch: str, base: str) -> str:
    """Create ``branch`` off ``base`` with one commit and push it; return the sha."""
    work = tmp_path / f"cloud-{branch.replace('/', '-')}"
    work.mkdir()
    _git(work, "clone", str(remote), str(work))
    _git(work, "checkout", "-b", branch, base)
    (work / "feature.txt").write_text("cloud work\n", encoding="utf-8")
    _git(work, "add", "feature.txt")
    _git(work, "commit", "-m", "cloud change")
    _git(work, "push", "origin", f"{branch}:{branch}")
    return _git(work, "rev-parse", "HEAD")


def _task() -> FactoryTask:
    return FactoryTask(
        title="Cloud task",
        target_repository="cartunduaga06/ai-factory-lab",
        source=TaskSource("github", "cartunduaga06/ai-factory-lab", 15),
    )


def _scenario(tmp_path: Path) -> tuple[Path, Path, Workspace, str]:
    """A remote, a source checkout, a workspace, and a pushed commit on its branch."""
    remote = _bare_remote(tmp_path)
    _seed_remote(tmp_path, remote)
    source = _source_checkout(tmp_path, remote)
    base = _git(source, "rev-parse", "origin/main")
    workspace = new_workspace(_task(), str(tmp_path / "workspaces"))
    commit = _push_branch_with_commit(remote, tmp_path, workspace.branch, base)
    return remote, source, workspace, commit


def test_materialize_fetches_and_checks_out_the_exact_commit(tmp_path: Path) -> None:
    _remote, source, workspace, commit = _scenario(tmp_path)
    provider = GitCloudRevisionProvider(str(source))
    revision = RemoteRevision(commit, workspace.branch, workspace.repository_slug)

    provider.materialize(workspace, revision)

    worktree = Path(workspace.path)
    assert _git(worktree, "rev-parse", "HEAD") == commit
    assert _git(worktree, "rev-parse", "--abbrev-ref", "HEAD") == workspace.branch
    assert (worktree / "feature.txt").read_text(encoding="utf-8") == "cloud work\n"


def test_materialize_reuses_an_existing_worktree_at_the_exact_commit(tmp_path: Path) -> None:
    """Re-collection must be idempotent: a second pass is a no-op, not an error."""
    _remote, source, workspace, commit = _scenario(tmp_path)
    provider = GitCloudRevisionProvider(str(source))
    revision = RemoteRevision(commit, workspace.branch, workspace.repository_slug)

    provider.materialize(workspace, revision)
    provider.materialize(workspace, revision)

    assert _git(Path(workspace.path), "rev-parse", "HEAD") == commit


def test_materialize_advances_an_existing_worktree_by_fast_forward(tmp_path: Path) -> None:
    """The dispatch-time worktree is at the base; materialising fast-forwards it."""
    from factory.integrations.workspace.git import GitWorktreeWorkspaceProvisioner

    remote = _bare_remote(tmp_path)
    _seed_remote(tmp_path, remote)
    source = _source_checkout(tmp_path, remote)
    base = _git(source, "rev-parse", "origin/main")
    task = _task()
    workspace = new_workspace(task, str(tmp_path / "workspaces"))
    commit = _push_branch_with_commit(remote, tmp_path, workspace.branch, base)

    GitWorktreeWorkspaceProvisioner(str(source)).prepare(task, workspace)
    assert _git(Path(workspace.path), "rev-parse", "HEAD") == base

    provider = GitCloudRevisionProvider(str(source))
    provider.materialize(
        workspace, RemoteRevision(commit, workspace.branch, workspace.repository_slug)
    )
    assert _git(Path(workspace.path), "rev-parse", "HEAD") == commit


def test_materialize_refuses_when_the_branch_moved(tmp_path: Path) -> None:
    """A branch that no longer resolves to the claimed sha must fail closed."""
    _remote, source, workspace, commit = _scenario(tmp_path)
    provider = GitCloudRevisionProvider(str(source))
    # The claimed sha is a valid-looking but different commit.
    wrong = "0" * 40
    assert wrong != commit
    with pytest.raises(CloudRevisionError):
        provider.materialize(
            workspace, RemoteRevision(wrong, workspace.branch, workspace.repository_slug)
        )
    assert not Path(workspace.path).exists()


def test_materialize_refuses_an_unpushed_branch(tmp_path: Path) -> None:
    remote = _bare_remote(tmp_path)
    _seed_remote(tmp_path, remote)
    source = _source_checkout(tmp_path, remote)
    workspace = new_workspace(_task(), str(tmp_path / "workspaces"))
    provider = GitCloudRevisionProvider(str(source))
    with pytest.raises(CloudRevisionError):
        provider.materialize(
            workspace,
            RemoteRevision("a" * 40, workspace.branch, workspace.repository_slug),
        )
    assert not Path(workspace.path).exists()


@pytest.mark.parametrize(
    "sha",
    ["", "not-a-sha", "abc123", "xyz" * 15, "a" * 200],
)
def test_materialize_refuses_an_unsafe_sha(tmp_path: Path, sha: str) -> None:
    remote = _bare_remote(tmp_path)
    _seed_remote(tmp_path, remote)
    source = _source_checkout(tmp_path, remote)
    workspace = new_workspace(_task(), str(tmp_path / "workspaces"))
    provider = GitCloudRevisionProvider(str(source))
    # The domain rejects the blank sha at construction; the provider rejects the
    # rest. Either way nothing is materialised.
    with pytest.raises((ValueError, CloudRevisionError)):
        provider.materialize(
            workspace, RemoteRevision(sha, workspace.branch, workspace.repository_slug)
        )
    assert not Path(workspace.path).exists()


@pytest.mark.parametrize("branch", ["main", "master", "HEAD", "-x/y", "noslash"])
def test_materialize_refuses_a_protected_or_malformed_branch(tmp_path: Path, branch: str) -> None:
    remote = _bare_remote(tmp_path)
    _seed_remote(tmp_path, remote)
    source = _source_checkout(tmp_path, remote)
    workspace = Workspace(
        workspace_id="ws-1",
        repository_slug="cartunduaga06/ai-factory-lab",
        branch=branch,
        path=str(tmp_path / "workspaces" / "ws-1"),
    )
    provider = GitCloudRevisionProvider(str(source))
    with pytest.raises(CloudRevisionError):
        provider.materialize(
            workspace,
            RemoteRevision("a" * 40, branch, workspace.repository_slug),
        )


def test_materialize_refuses_a_repository_mismatch(tmp_path: Path) -> None:
    remote = _bare_remote(tmp_path)
    _seed_remote(tmp_path, remote)
    source = _source_checkout(tmp_path, remote)
    workspace = new_workspace(_task(), str(tmp_path / "workspaces"))
    provider = GitCloudRevisionProvider(str(source))
    with pytest.raises(CloudRevisionError):
        provider.materialize(
            workspace,
            RemoteRevision("a" * 40, workspace.branch, "someone/else"),
        )


def test_materialize_never_leaks_the_source_path_or_output(tmp_path: Path) -> None:
    """The sanitized error carries the workspace id only, not paths or git output."""
    remote = _bare_remote(tmp_path)
    _seed_remote(tmp_path, remote)
    source = _source_checkout(tmp_path, remote)
    workspace = new_workspace(_task(), str(tmp_path / "workspaces"))
    provider = GitCloudRevisionProvider(str(source))
    with pytest.raises(CloudRevisionError) as caught:
        provider.materialize(
            workspace,
            RemoteRevision("b" * 40, workspace.branch, workspace.repository_slug),
        )
    message = str(caught.value)
    assert str(source) not in message
    assert "git" not in message.lower().replace("revision", "")


def test_backend_enum_has_local_and_cloud() -> None:
    assert AgentBackend.LOCAL.value == "local"
    assert AgentBackend.CLOUD.value == "cloud"
    assert AgentBackend("local") is AgentBackend.LOCAL
    assert AgentBackend("cloud") is AgentBackend.CLOUD


def test_cloud_provisioner_does_not_mutate_source_origin(tmp_path: Path) -> None:
    remote = _bare_remote(tmp_path)
    _seed_remote(tmp_path, remote)
    source = _source_checkout(tmp_path, remote)
    original = _git(source, "remote", "get-url", "origin")
    task = _task()
    workspace = new_workspace(task, str(tmp_path / "workspaces"))

    CloudWorkspaceProvisioner(str(source), base_ref="main").prepare(task, workspace)

    assert _git(source, "remote", "get-url", "origin") == original
    assert _git(Path(workspace.path), "remote", "get-url", "origin") == original


def test_materialize_refuses_a_network_remote_for_another_repository(tmp_path: Path) -> None:
    remote = _bare_remote(tmp_path)
    _seed_remote(tmp_path, remote)
    source = _source_checkout(tmp_path, remote)
    _git(source, "remote", "set-url", "origin", "https://github.com/someone/else.git")
    workspace = new_workspace(_task(), str(tmp_path / "workspaces"))
    provider = GitCloudRevisionProvider(str(source))

    with pytest.raises(CloudRevisionError):
        provider.materialize(
            workspace,
            RemoteRevision("a" * 40, workspace.branch, workspace.repository_slug),
        )


def test_authenticated_fetch_uses_askpass_without_token_in_argv(tmp_path: Path) -> None:
    token = "private-read-token-must-not-leak"
    revision = RemoteRevision(
        "a" * 40,
        "factory/task/ws",
        "cartunduaga06/ai-factory-lab",
    )

    class RecordingProvider(GitCloudRevisionProvider):
        def __init__(self) -> None:
            super().__init__(str(tmp_path), read_token=token)
            self.fetch_argv: list[str] = []
            self.fetch_env: dict[str, str] = {}
            self.askpass_contents = ""

        def _run(
            self,
            args: list[str],
            *,
            cwd: Path,
            workspace_id: str,
            allow_failure: bool = False,
            env: dict[str, str] | None = None,
        ) -> int:
            del cwd, workspace_id, allow_failure
            self.fetch_argv = list(args)
            self.fetch_env = dict(env or {})
            askpass = self.fetch_env.get("GIT_ASKPASS")
            if askpass:
                self.askpass_contents = Path(askpass).read_text()
            return 0

        def _capture(self, args: list[str], *, cwd: Path, workspace_id: str) -> str | None:
            del args, cwd, workspace_id
            return revision.commit_sha

    provider = RecordingProvider()
    provider._fetch("ws", revision)

    assert token not in " ".join(provider.fetch_argv)
    assert token not in provider.askpass_contents
    assert provider.fetch_env["AI_FACTORY_GIT_TOKEN"] == token
    askpass = provider.fetch_env["GIT_ASKPASS"]
    assert askpass
    assert not Path(askpass).exists()
