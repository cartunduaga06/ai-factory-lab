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


# -- transport security ----------------------------------------------------


class _PushSpy:
    """Records factory-controlled git commands and authenticated-push entries."""

    def __init__(self) -> None:
        self.commands: list[list[str]] = []
        self.token_pushes: list[str] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_run = GitWorkspacePublisher._run

        def recording_run(
            inner_self: GitWorkspacePublisher,
            args: list[str],
            workspace: Workspace,
            **kwargs: object,
        ) -> None:
            self.commands.append(list(args))
            real_run(inner_self, args, workspace, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(GitWorkspacePublisher, "_run", recording_run)
        monkeypatch.setattr(
            GitWorkspacePublisher,
            "_push_with_token",
            lambda inner_self, workspace, refspec: self.token_pushes.append(refspec),
        )

    def pushed(self) -> bool:
        return any("push" in args for args in self.commands)


def test_plain_http_remote_is_refused_before_any_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    # Plaintext HTTP: the write credential must never cross an unencrypted
    # transport. Built by concatenation so no traceback source line carries the
    # literal URL value.
    unsafe_url = "http" + "://example.invalid/target.git"
    _git(source, "remote", "set-url", "origin", unsafe_url)

    spy = _PushSpy()
    spy.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    assert not spy.pushed()
    assert spy.token_pushes == []
    for text in (SECRET, "http://example.invalid/target.git", "example.invalid", "http://"):
        assert text not in str(error)
        assert text not in repr(error)
        assert text not in formatted
    assert error.__cause__ is None and error.__context__ is None


def test_http_remote_with_userinfo_secret_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    unsafe_url = "http://" + "vault_user_93726" + ":" + SECRET + "@example.invalid/target.git"
    _git(source, "remote", "set-url", "origin", unsafe_url)

    spy = _PushSpy()
    spy.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    assert not spy.pushed()
    assert spy.token_pushes == []
    for text in (SECRET, "vault_user_93726", "example.invalid", "http://"):
        assert text not in str(error)
        assert text not in repr(error)
        assert text not in formatted
    assert error.__cause__ is None and error.__context__ is None


def test_https_remote_reaches_the_authenticated_push_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    # TLS is the only transport the write credential may use. The push itself is
    # intercepted, so no live network call is made.
    _git(source, "remote", "set-url", "origin", "https://github.com/example/target.git")

    spy = _PushSpy()
    spy.install(monkeypatch)

    _publisher(write_token=SECRET).publish(_task(), run)

    refspec = f"refs/heads/{workspace.branch}:refs/heads/{workspace.branch}"
    assert spy.token_pushes == [refspec]
    # The authenticated path owns the push; the plaintext path did not run one.
    assert not spy.pushed()


# -- effective push destination pinning ------------------------------------
#
# A completed agent can edit remote configuration, so the publisher must inspect
# what Git will *actually* push to — pushurl, insteadOf/pushInsteadOf — and pin it
# to one allowed HTTPS GitHub target before the write credential is exposed.

_SAFE_GITHUB_URL = "https://" + "github.com/example/target.git"


class _PushRecorder:
    """Records every git invocation and intercepts only the authenticated push.

    Repository-local git commands still run for real (so a commit can be made),
    but an authenticated network push is short-circuited, so no test performs a
    network request.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], dict[str, str]]] = []
        self.authenticated: list[tuple[list[str], dict[str, str]]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_execute = GitWorkspacePublisher._execute

        def recording_execute(
            inner_self: GitWorkspacePublisher,
            args: list[str],
            workspace: Workspace,
            *,
            env: dict[str, str],
        ) -> subprocess.CompletedProcess[bytes]:
            self.calls.append((list(args), dict(env)))
            if "push" in args and "GIT_ASKPASS" in env:
                self.authenticated.append((list(args), dict(env)))
                return subprocess.CompletedProcess(["git", *args], 0, b"", b"")
            return real_execute(inner_self, args, workspace, env=env)

        monkeypatch.setattr(GitWorkspacePublisher, "_execute", recording_execute)

    def child_envs(self) -> list[dict[str, str]]:
        return [env for _, env in self.calls]

    def pushed(self) -> bool:
        return any("push" in args for args, _ in self.calls)


def _assert_refusal_clean(error: BaseException, *forbidden: str) -> None:
    formatted = "".join(traceback.format_exception(error))
    for text in forbidden:
        assert text not in str(error)
        assert text not in repr(error)
        assert text not in formatted
    assert error.__cause__ is None
    assert error.__context__ is None


def _network_setup(tmp_path: Path, url: str) -> tuple[Path, Path, Workspace, AgentRun]:
    source, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")
    _git(source, "remote", "set-url", "origin", url)
    return source, remote, workspace, run


def _source_for(workspace: Workspace) -> Path:
    """The main checkout backing ``workspace``'s linked worktree."""
    common = (
        subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=workspace.path,
            capture_output=True,
            env=_env(Path(workspace.path)),
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    return Path(common).resolve().parent


def test_malicious_pushurl_is_detected_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # remote.origin.url is safe; remote.origin.pushurl is malicious. Inspecting
    # only remote.<name>.url would miss it and leak the token to the wrong host.
    evil_url = "https://" + "evil.example/example/target.git"
    source, _, workspace, run = _network_setup(tmp_path, _SAFE_GITHUB_URL)
    _git(source, "config", "--add", "remote.origin.pushurl", evil_url)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert not recorder.pushed()
    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "evil.example", evil_url, _SAFE_GITHUB_URL)


def test_multiple_network_push_destinations_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _, workspace, run = _network_setup(tmp_path, _SAFE_GITHUB_URL)
    # Two distinct push URLs: the branch and credential must never fan out.
    _git(source, "config", "--add", "remote.origin.pushurl", _SAFE_GITHUB_URL)
    _git(source, "config", "--add", "remote.origin.pushurl", _SAFE_GITHUB_URL + "/mirror")

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, _SAFE_GITHUB_URL)


def test_wrong_repository_effective_url_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong_repo = "https://github.com/example/another-repo.git"
    _, _, workspace, run = _network_setup(tmp_path, wrong_repo)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "another-repo", wrong_repo)


def test_wrong_owner_effective_url_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrong_owner = "https://github.com/attacker/target.git"
    _, _, workspace, run = _network_setup(tmp_path, wrong_owner)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "attacker", wrong_owner)


def test_wrong_host_https_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # HTTPS alone is not sufficient: the host must be the configured allowed host.
    evil_host = "https://evil.example/example/target.git"
    _, _, workspace, run = _network_setup(tmp_path, evil_host)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "evil.example", evil_host)


def test_additional_path_components_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    nested = "https://github.com/example/target/evilpath.git"
    _, _, workspace, run = _network_setup(tmp_path, nested)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "evilpath", nested)


def test_query_string_on_effective_url_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with_query = "https://github.com/example/target.git?x=1"
    _, _, workspace, run = _network_setup(tmp_path, with_query)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, with_query, "x=1")


def test_insteadof_rewrite_is_detected_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A literal, safe origin URL is rewritten by configuration to a malicious
    # host. The publisher must validate the effective destination, not the URL
    # written in remote.origin.url.
    _, _, workspace, run = _network_setup(tmp_path, _SAFE_GITHUB_URL)
    _git(
        _source_for(workspace),
        "config",
        "url.https://evil.example/.insteadOf",
        "https://github.com/",
    )

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "evil.example")


def test_pushinsteadof_rewrite_is_detected_and_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, _, workspace, run = _network_setup(tmp_path, _SAFE_GITHUB_URL)
    _git(source, "config", "url.https://evil.example/.pushInsteadOf", "https://github.com/")

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "evil.example")


def test_scp_style_network_remote_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scp = "git@github.com:example/target.git"
    _, _, workspace, run = _network_setup(tmp_path, scp)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    # No child process may receive ambient SSH credentials or the write token.
    assert not recorder.pushed()
    assert recorder.authenticated == []
    for env in recorder.child_envs():
        assert "SSH_AUTH_SOCK" not in env
        assert "GITHUB_WRITE_TOKEN" not in env
        assert "GIT_FACTORY_PASSWORD" not in env
    _assert_refusal_clean(caught.value, SECRET, "git@github.com", scp)


def test_ssh_url_network_remote_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ssh = "ssh://git@github.com/example/target.git"
    _, _, workspace, run = _network_setup(tmp_path, ssh)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    for env in recorder.child_envs():
        assert "SSH_AUTH_SOCK" not in env
        assert "GITHUB_WRITE_TOKEN" not in env
        assert "GIT_FACTORY_PASSWORD" not in env
    _assert_refusal_clean(caught.value, SECRET, "ssh://", ssh)


def test_git_protocol_network_remote_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    git_url = "git://github.com/example/target.git"
    _, _, workspace, run = _network_setup(tmp_path, git_url)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "git://", git_url)


def test_https_remote_with_port_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with_port = "https://github.com:8443/example/target.git"
    _, _, workspace, run = _network_setup(tmp_path, with_port)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "8443", with_port)


# -- safe HTTPS happy path -------------------------------------------------


def test_safe_https_push_uses_remote_name_and_isolated_refspec(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, _, workspace, run = _network_setup(tmp_path, _SAFE_GITHUB_URL)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    _publisher(write_token=SECRET).publish(_task(), run)

    assert len(recorder.authenticated) == 1
    args, env = recorder.authenticated[0]
    refspec = f"refs/heads/{workspace.branch}:refs/heads/{workspace.branch}"

    # Pushed by remote name — the same resolution that was validated — with an
    # exact, non-force refspec, and no main/master.
    assert args == [
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "credential.helper=",
        "push",
        "origin",
        refspec,
    ]
    assert "--force" not in args and "-f" not in args
    assert not any("main" in arg or "master" in arg for arg in args)
    # The token is never in argv or a URL; the URL is not in argv at all.
    assert SECRET not in " ".join(args)
    assert "https://" not in " ".join(args)
    # The credential travels only through the askpass environment.
    assert "GIT_ASKPASS" in env
    assert env["GIT_FACTORY_PASSWORD"] == SECRET
    assert "GITHUB_WRITE_TOKEN" not in env


def test_local_bare_remote_still_works_without_a_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, remote, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    revision = _publisher().publish(_task(), run)

    # A local destination needs no credential and is pushed normally.
    assert recorder.authenticated == []
    branches = _git(remote, "branch", "--list", "--format=%(refname:short)").stdout.decode()
    assert branches.strip() == workspace.branch
    assert _rev(remote, workspace.branch) == revision.commit_sha
    for env in recorder.child_envs():
        assert "GITHUB_WRITE_TOKEN" not in env
        assert "GIT_FACTORY_PASSWORD" not in env
        assert "GIT_ASKPASS" not in env


def test_push_follows_the_validated_local_destination_under_a_rewrite(tmp_path: Path) -> None:
    # A local remote whose effective destination is redirected by insteadOf. The
    # publisher validates the *effective* destination (a local path) and pushes by
    # remote name; Git resolves the name with the same algorithm, so the branch
    # lands exactly where validation said — not at the literal origin URL.
    source, _, workspace, run = _setup(tmp_path)
    (Path(workspace.path) / "feature.py").write_text("x\n", encoding="utf-8")

    redirected = tmp_path / "redirected.git"
    redirected.mkdir()
    _git(redirected, "init", "--bare", "-b", "main")
    literal = tmp_path / "literal.git"
    literal.mkdir()
    _git(literal, "init", "--bare", "-b", "main")
    _git(source, "remote", "set-url", "origin", str(literal))
    _git(source, "config", f"url.{redirected.as_posix()}.insteadOf", str(literal))

    revision = _publisher().publish(_task(), run)

    redirected_branches = _git(redirected, "branch", "--list", "--format=%(refname:short)")
    assert redirected_branches.stdout.decode().strip() == workspace.branch
    assert _rev(redirected, workspace.branch) == revision.commit_sha
    # The literal destination was never touched.
    literal_branches = _git(literal, "branch", "--list", "--format=%(refname:short)")
    assert literal_branches.stdout.decode() == ""


def test_malformed_network_url_is_refused_not_raised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # An unterminated IPv6 literal makes urlsplit raise; that must surface as a
    # sanitized refusal, never as an escaping ValueError.
    malformed = "https://[::1/example/target.git"
    _, _, workspace, run = _network_setup(tmp_path, malformed)

    recorder = _PushRecorder()
    recorder.install(monkeypatch)

    with pytest.raises(UnsafeRemoteError) as caught:
        _publisher(write_token=SECRET).publish(_task(), run)

    assert recorder.authenticated == []
    _assert_refusal_clean(caught.value, SECRET, "::1", malformed)
