"""Tests for the concrete Git workspace publisher.

Every test builds a disposable source repository, one of its worktrees and a bare
remote under ``tmp_path``. No test touches a real product repository, the network,
the machine's global Git configuration, or an actual credential.
"""

from __future__ import annotations

import os
import subprocess
import traceback
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, RunStatus
from factory.domain.errors import (
    PublicationError,
    RevisionNotPublishableError,
    UnsafeRemoteError,
)
from factory.domain.models import AgentRun, FactoryTask, TaskSource, Workspace
from factory.integrations.workspace.git_publish import (
    GitWorkspacePublisher,
    commit_message_for,
)

SECRET = "MY_PRIVATE_PUSH_PASSWORD_93726"

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


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, env=_env(cwd)
    )


def _source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "initial")
    return source


def _bare_remote(tmp_path: Path) -> Path:
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    return remote


def _worktree(source: Path, branch: str, path: Path) -> None:
    _git(source, "worktree", "add", "-b", branch, str(path))


def _task() -> FactoryTask:
    return FactoryTask(
        title="Task",
        target_repository="example/target",
        source=TaskSource("github", "example/control", 1),
    )


def _run(workspace: Workspace) -> AgentRun:
    return AgentRun(
        task_id="task-1",
        adapter=AgentKind.OTHER,
        run_id="run-1",
        status=RunStatus.SUCCEEDED,
        workspace=workspace,
    )


def _rev(path: Path, ref: str) -> str:
    return _git(path, "rev-parse", ref).stdout.decode().strip()


def _setup(
    tmp_path: Path, *, branch: str = "factory/task-1/ws-1", repository_slug: str = "example/target"
) -> tuple[Path, Path, Workspace, AgentRun]:
    source = _source_repo(tmp_path)
    remote = _bare_remote(tmp_path)
    worktree = tmp_path / "ws-1"
    _worktree(source, branch, worktree)
    _git(source, "remote", "add", "origin", str(remote))
    workspace = Workspace(
        repository_slug=repository_slug,
        branch=branch,
        path=str(worktree),
    )
    return source, remote, workspace, _run(workspace)


def _publisher(**kwargs: object) -> GitWorkspacePublisher:
    return GitWorkspacePublisher(**kwargs)  # type: ignore[arg-type]


# -- happy path ------------------------------------------------------------


def test_commit_is_created_only_inside_the_isolated_branch(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    worktree = Path(workspace.path)
    (worktree / "feature.py").write_text("print('x')\n", encoding="utf-8")

    before_main = _rev(source, "main")
    revision = _publisher().publish(_task(), run)

    assert revision.branch == workspace.branch
    assert len(revision.commit_sha) == 40
    # The source checkout is untouched: main has not moved and is not checked out.
    assert _rev(source, "main") == before_main
    assert worktree.joinpath("feature.py").exists()
    # The commit is on the isolated branch and contains the change.
    files = _git(worktree, "show", "--name-only", "--format=", "HEAD").stdout.decode()
    assert "feature.py" in files


def test_source_checkout_and_default_branch_are_unchanged(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    source_head_before = _rev(source, "HEAD")

    _publisher().publish(_task(), run)

    assert _rev(source, "HEAD") == source_head_before
    assert _rev(source, "main") == source_head_before


def test_push_creates_only_the_workspace_branch_on_the_remote(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")

    revision = _publisher().publish(_task(), run)

    branches = _git(remote, "branch", "--list", "--format=%(refname:short)").stdout.decode()
    assert branches.strip() == workspace.branch
    assert _rev(remote, workspace.branch) == revision.commit_sha
    # The remote's main ref was never created or moved.
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "refs/heads/main"],
        cwd=str(remote),
        capture_output=True,
        env=_env(remote),
    )
    assert result.returncode != 0


def test_commit_message_is_deterministic_and_uses_no_task_body(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    task = _task()
    task.body = "UNTRUSTED BODY TEXT THAT MUST NOT BE A COMMIT MESSAGE"

    _publisher().publish(task, run)

    message = _git(Path(workspace.path), "log", "-1", "--format=%s").stdout.decode().strip()
    assert message == commit_message_for(task)
    assert task.task_id in message
    assert "UNTRUSTED" not in message


# -- idempotency -----------------------------------------------------------


def test_retry_reuses_the_same_commit_and_push(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    publisher = _publisher()
    task = _task()

    first = publisher.publish(task, run)
    second = publisher.publish(task, run)

    assert second.commit_sha == first.commit_sha
    assert second.branch == first.branch
    # No second commit was created: HEAD is unchanged.
    assert _rev(Path(workspace.path), "HEAD") == first.commit_sha


def test_push_retry_is_idempotent(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    publisher = _publisher()
    task = _task()
    first = publisher.publish(task, run)
    # Re-publishing (nothing changed) must succeed and leave the remote identical.
    second = publisher.publish(task, run)
    assert second == first


def test_no_changes_and_no_factory_commit_is_refused(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)

    with pytest.raises(RevisionNotPublishableError):
        _publisher().publish(_task(), run)


# -- rejections ------------------------------------------------------------


def test_workspace_on_the_wrong_branch_is_rejected(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    remote = _bare_remote(tmp_path)
    worktree = tmp_path / "ws-1"
    _worktree(source, "factory/other-branch", worktree)
    _git(source, "remote", "add", "origin", str(remote))
    workspace = Workspace(
        repository_slug="example/target",
        branch="factory/task-1/ws-1",  # declared branch differs from actual HEAD
        path=str(worktree),
    )

    with pytest.raises(PublicationError):
        _publisher().publish(_task(), _run(workspace))


def test_protected_branch_is_rejected(tmp_path: Path) -> None:
    source = _source_repo(tmp_path)
    remote = _bare_remote(tmp_path)
    _git(source, "remote", "add", "origin", str(remote))
    workspace = Workspace(repository_slug="example/target", branch="main", path=str(source))

    with pytest.raises(PublicationError):
        _publisher().publish(_task(), _run(workspace))


def test_workspace_for_another_repository_is_rejected(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path, repository_slug="example/other")
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")

    with pytest.raises(PublicationError):
        _publisher().publish(_task(), run)


def test_missing_workspace_directory_is_rejected(tmp_path: Path) -> None:
    workspace = Workspace(
        repository_slug="example/target",
        branch="factory/task-1/ws-1",
        path=str(tmp_path / "does-not-exist"),
    )

    with pytest.raises(PublicationError):
        _publisher().publish(_task(), _run(workspace))


def test_credential_bearing_remote_url_is_rejected(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    # A remote URL carrying userinfo is the classic credential-in-URL leak vector.
    _git(
        source, "remote", "set-url", "origin", f"https://x-access-token:{SECRET}@example.com/r.git"
    )

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher().publish(_task(), run)

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    for text in (SECRET, "x-access-token", "example.com"):
        assert text not in str(error)
        assert text not in repr(error)
        assert text not in formatted
    assert error.__cause__ is None and error.__context__ is None


def test_https_push_without_a_write_credential_is_refused(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    _git(source, "remote", "set-url", "origin", "https://github.com/example/target.git")

    with pytest.raises(PublicationError):
        _publisher().publish(_task(), run)


def test_git_failure_is_sanitized(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    # Point origin at a path that does not exist so the push fails.
    _git(source, "remote", "set-url", "origin", str(tmp_path / "missing-remote.git"))

    with pytest.raises(PublicationError) as caught:
        _publisher().publish(_task(), run)

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    for text in (SECRET, str(tmp_path / "missing-remote.git")):
        assert text not in str(error)
        assert text not in repr(error)
        assert text not in formatted
    assert error.__cause__ is None and error.__context__ is None


def test_hooks_do_not_receive_the_write_credential(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    worktree = Path(workspace.path)
    (worktree / "feature.py").write_text("x\n", encoding="utf-8")
    # A repository-controlled hook that would capture the write credential if the
    # factory let it run in the commit environment.
    hooks = source / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    marker = tmp_path / "hook-ran"
    pre_commit = hooks / "pre-commit"
    pre_commit.write_text(
        f'#!/bin/sh\nprintf "%s" "${{GIT_FACTORY_PASSWORD:-none}}" > "{marker}"\n',
        encoding="utf-8",
    )
    pre_commit.chmod(0o755)

    _publisher(write_token=SECRET, remote="origin").publish(_task(), run)

    # The hook was disabled for the factory commit, so it never ran with secrets.
    assert not marker.exists()
    assert SECRET not in (marker.read_text(encoding="utf-8") if marker.exists() else "")


def test_force_push_is_never_used(tmp_path: Path) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    # Advance the remote branch to a divergent commit.
    other = tmp_path / "other"
    _git(tmp_path, "clone", str(remote), str(other))
    (other / "other.txt").write_text("diverged\n", encoding="utf-8")
    _git(other, "add", "other.txt")
    _git(other, "commit", "-m", "diverge")
    _git(other, "push", "origin", f"HEAD:refs/heads/{workspace.branch}")

    # A non-fast-forward push must fail rather than overwrite the remote history.
    with pytest.raises(PublicationError):
        _publisher().publish(_task(), run)
    assert _rev(remote, workspace.branch) == _rev(other, "HEAD")
