"""Read-only Git evidence for clean worker recovery."""

from __future__ import annotations

import subprocess
from pathlib import Path

from factory.domain.models import Workspace
from factory.integrations.workspace.recovery import git_workspace_is_clean_unpublished


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )


def _workspace(tmp_path: Path) -> Workspace:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.invalid")
    (repo / "README.md").write_text("baseline\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "baseline")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    branch = "factory/task/workspace"
    _git(repo, "checkout", "-b", branch)
    return Workspace(repository_slug="example/repo", path=str(repo), branch=branch)


def test_clean_workspace_without_ahead_commits_is_recoverable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    assert git_workspace_is_clean_unpublished(workspace, "main")


def test_dirty_or_committed_workspace_is_not_recoverable(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    repo = Path(workspace.path)
    (repo / "dirty.txt").write_text("dirty\n")
    assert not git_workspace_is_clean_unpublished(workspace, "main")

    (repo / "dirty.txt").unlink()
    (repo / "committed.txt").write_text("commit\n")
    _git(repo, "add", "committed.txt")
    _git(repo, "commit", "-m", "agent change")
    assert not git_workspace_is_clean_unpublished(workspace, "main")
