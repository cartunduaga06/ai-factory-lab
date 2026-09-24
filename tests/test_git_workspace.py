"""Tests for the concrete Git worktree workspace provisioner.

Every test builds its own disposable Git repository under ``tmp_path``. No test
touches a real product repository, the network, or the machine's global Git
configuration.
"""

from __future__ import annotations

import os
import subprocess
import traceback
from pathlib import Path

import pytest

from factory.domain.enums import TaskStatus
from factory.domain.errors import WorkspaceProvisioningError
from factory.domain.models import FactoryTask, TaskSource, new_workspace
from factory.integrations.workspace.git import GitWorktreeWorkspaceProvisioner

SECRET = "ghp_supersecrettokenmustnotleak"

# A clean author identity so commits in the disposable repo are deterministic.
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


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, env=_env(cwd))


def _source_repo(tmp_path: Path) -> Path:
    """A disposable repository with one commit on ``main``."""
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "initial")
    return source


def _task(number: int = 1) -> FactoryTask:
    return FactoryTask(
        title="Task",
        target_repository="example/target",
        status=TaskStatus.READY,
        source=TaskSource("github", "example/control", number),
    )


def _branch_of(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(path),
        check=True,
        capture_output=True,
        env=_env(path),
    )
    return result.stdout.decode().strip()


def _head_of(path: Path) -> str:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(path),
        check=True,
        capture_output=True,
        env=_env(path),
    )
    return result.stdout.decode().strip()


def _provisioner(source: Path) -> GitWorktreeWorkspaceProvisioner:
    return GitWorktreeWorkspaceProvisioner(str(source))


# -- happy path ------------------------------------------------------------


def test_worktree_is_created_on_an_isolated_branch(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    root = tmp_path / "workspaces"
    task = _task()
    workspace = new_workspace(task, str(root))

    prepared = _provisioner(source).prepare(task, workspace)

    assert prepared is workspace
    assert Path(workspace.path).is_dir()
    assert (Path(workspace.path) / ".git").exists()
    assert _branch_of(Path(workspace.path)) == workspace.branch
    assert workspace.branch != "main"
    assert workspace.branch.startswith("factory/")


def test_source_checkout_is_left_on_its_original_branch(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    root = tmp_path / "workspaces"
    task = _task()

    before = _branch_of(source)
    _provisioner(source).prepare(task, new_workspace(task, str(root)))
    after = _branch_of(source)

    assert before == after == "main"


def test_two_workspaces_for_the_same_task_are_isolated(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    root = tmp_path / "workspaces"
    task = _task()
    provisioner = _provisioner(source)

    first = new_workspace(task, str(root))
    second = new_workspace(task, str(root))
    provisioner.prepare(task, first)
    provisioner.prepare(task, second)

    assert first.workspace_id != second.workspace_id
    assert first.branch != second.branch
    assert first.path != second.path
    assert _branch_of(Path(first.path)) == first.branch
    assert _branch_of(Path(second.path)) == second.branch


def test_preparing_the_same_workspace_twice_is_idempotent(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    root = tmp_path / "workspaces"
    task = _task()
    provisioner = _provisioner(source)
    workspace = new_workspace(task, str(root))

    provisioner.prepare(task, workspace)
    provisioner.prepare(task, workspace)

    assert Path(workspace.path).is_dir()
    assert _branch_of(Path(workspace.path)) == workspace.branch


def test_preparation_creates_no_commit(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    root = tmp_path / "workspaces"
    task = _task()
    workspace = new_workspace(task, str(root))

    _provisioner(source).prepare(task, workspace)

    # The new branch points at the same commit as the source HEAD: the factory
    # never commits agent work itself.
    assert _head_of(source) == _head_of(Path(workspace.path))


# -- rejection / safety ----------------------------------------------------


def test_pre_existing_mismatched_workspace_is_rejected(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    root = tmp_path / "workspaces"
    task = _task()
    provisioner = _provisioner(source)

    # A plain directory (not a worktree) already exists at the workspace path.
    workspace = new_workspace(task, str(root))
    Path(workspace.path).mkdir(parents=True)
    (Path(workspace.path) / "leftover.txt").write_text("stale", encoding="utf-8")

    with pytest.raises(WorkspaceProvisioningError):
        provisioner.prepare(task, workspace)


def test_pre_existing_worktree_on_wrong_branch_is_rejected(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    root = tmp_path / "workspaces"
    task = _task()
    provisioner = _provisioner(source)

    workspace = new_workspace(task, str(root))
    # A worktree exists at the path, but on a different branch than expected.
    _git(source, "worktree", "add", "-b", "factory/someone-else", workspace.path)

    with pytest.raises(WorkspaceProvisioningError):
        provisioner.prepare(task, workspace)


def test_missing_source_checkout_is_a_sanitized_failure(tmp_path: Path) -> None:
    task = _task()
    workspace = new_workspace(task, str(tmp_path / "workspaces"))
    provisioner = GitWorktreeWorkspaceProvisioner(str(tmp_path / "does-not-exist"))

    with pytest.raises(WorkspaceProvisioningError) as caught:
        provisioner.prepare(task, workspace)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


def test_git_failure_does_not_leak_stderr_or_secrets(tmp_path: Path) -> None:
    """A failing git command must not expose stderr, a command line or a token."""
    source = _source_repo(tmp_path)
    # A remote URL with an embedded token is the classic leak vector: git echoes
    # the remote in some errors. Point the branch at a ref that does not exist by
    # asking for a base ref git cannot resolve.
    (source / ".git" / "config").write_text(
        f'[remote "origin"]\n\turl = https://x-access-token:{SECRET}@example.com/repo.git\n',
        encoding="utf-8",
    )
    task = _task()
    root = tmp_path / "workspaces"
    workspace = new_workspace(task, str(root))
    provisioner = GitWorktreeWorkspaceProvisioner(str(source), base_ref="refs/heads/does-not-exist")

    with pytest.raises(WorkspaceProvisioningError) as caught:
        provisioner.prepare(task, workspace)

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    for text in (SECRET, "x-access-token", "example.com", str(source)):
        assert text not in str(error)
        assert text not in repr(error)
        assert text not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None
