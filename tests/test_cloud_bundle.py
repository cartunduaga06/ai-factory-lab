"""Disposable local Git repositories exercise binary Cloud bundle import."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, RunStatus
from factory.domain.models import AgentRun, FactoryTask, RemoteRevision, TaskSource, Workspace
from factory.integrations.workspace.cloud_bundle import GitCloudBundleProvider
from factory.integrations.workspace.cloud_provisioner import CloudWorkspaceProvisioner
from factory.integrations.workspace.cloud_revision import CloudRevisionError
from factory.integrations.workspace.git_publish import GitWorkspacePublisher, commit_message_for
from factory.integrations.workspace.revision import GitWorkspaceRevisionInspector

BRANCH = "factory/task/ws"
REPO = "owner/private"


def git(path: Path, *args: str) -> str:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    result = subprocess.run(["git", *args], cwd=path, env=env, capture_output=True, check=True)
    return result.stdout.decode().strip()


def scenario(tmp_path: Path) -> tuple[GitCloudBundleProvider, Workspace, Path, str]:
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    (source / "README").write_text("base\n")
    (source / "delete-me").write_text("old\n")
    (source / "mode-me").write_text("#!/bin/sh\n")
    git(source, "add", "-A")
    git(source, "commit", "-m", "base")
    work = tmp_path / "work"
    git(source, "worktree", "add", "-b", BRANCH, str(work), "HEAD")
    workspace = Workspace("ws", REPO, BRANCH, str(work))
    return GitCloudBundleProvider(str(source)), workspace, source, git(work, "rev-parse", "HEAD")


def result_bundle(
    tmp_path: Path, data: bytes, change: Callable[[Path], None] | None = None
) -> tuple[bytes, str]:
    bundle = tmp_path / "input.bundle"
    bundle.write_bytes(data)
    remote = tmp_path / "sandbox"
    git(tmp_path, "clone", "--branch", BRANCH, str(bundle), str(remote))
    if change is None:
        (remote / "feature").write_text("change\n")
    else:
        change(remote)
    git(remote, "add", "-A")
    git(remote, "commit", "-m", "result")
    sha = git(remote, "rev-parse", "HEAD")
    result = tmp_path / "result.bundle"
    git(remote, "bundle", "create", str(result), f"refs/heads/{BRANCH}")
    return result.read_bytes(), sha


def test_bundle_round_trip_without_remote_or_credential(tmp_path: Path) -> None:
    provider, workspace, source, base = scenario(tmp_path)
    input_data, prepared = provider.prepare_input(workspace)
    assert prepared == base
    data, sha = result_bundle(tmp_path, input_data)
    provider.materialize_bundle(workspace, RemoteRevision(sha, BRANCH, REPO), data, base)
    work = Path(workspace.path)
    expected_tree = git(work, "rev-parse", f"{sha}^{{tree}}")
    assert git(work, "rev-parse", "HEAD") == base
    assert git(work, "status", "--porcelain")
    assert GitWorkspaceRevisionInspector().fingerprint(workspace) == expected_tree
    provider.materialize_bundle(workspace, RemoteRevision(sha, BRANCH, REPO), data, base)
    assert git(work, "rev-parse", "HEAD") == base
    assert git(work, "write-tree") == expected_tree
    assert GitWorkspaceRevisionInspector().fingerprint(workspace) == expected_tree
    assert git(source, "remote") == ""
    assert git(source, "for-each-ref", "--format=%(refname)", "refs/factory-cloud") == ""


def test_materializes_binary_delete_mode_and_symlink(tmp_path: Path) -> None:
    provider, workspace, _, base = scenario(tmp_path)
    input_data, _ = provider.prepare_input(workspace)

    def change(remote: Path) -> None:
        (remote / "README").write_text("updated\n")
        (remote / "delete-me").unlink()
        (remote / "mode-me").chmod(0o755)
        (remote / "binary").write_bytes(b"\x00\xff\x01\x80" * 128)
        (remote / "link").symlink_to("README")

    data, sha = result_bundle(tmp_path, input_data, change)
    provider.materialize_bundle(workspace, RemoteRevision(sha, BRANCH, REPO), data, base)
    work = Path(workspace.path)
    tree = git(work, "rev-parse", f"{sha}^{{tree}}")
    assert git(work, "rev-parse", "HEAD") == base
    assert git(work, "write-tree") == tree
    assert GitWorkspaceRevisionInspector().fingerprint(workspace) == tree
    assert (work / "binary").read_bytes() == b"\x00\xff\x01\x80" * 128
    assert not (work / "delete-me").exists()
    assert (work / "link").is_symlink()
    assert os.access(work / "mode-me", os.X_OK)
    provider.materialize_bundle(workspace, RemoteRevision(sha, BRANCH, REPO), data, base)
    assert git(work, "write-tree") == tree


def test_materialized_tree_publishes_as_factory_commit(tmp_path: Path) -> None:
    provider, workspace, source, base = scenario(tmp_path)
    input_data, _ = provider.prepare_input(workspace)
    data, cloud_sha = result_bundle(tmp_path, input_data)
    provider.materialize_bundle(workspace, RemoteRevision(cloud_sha, BRANCH, REPO), data, base)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))
    git(source, "remote", "add", "origin", str(remote))
    task = FactoryTask(title="Task", target_repository=REPO, source=TaskSource("github", REPO, 1))
    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.OTHER,
        status=RunStatus.SUCCEEDED,
        workspace=workspace,
    )
    run.validated_revision = GitWorkspaceRevisionInspector().fingerprint(workspace)

    published = GitWorkspacePublisher().publish(task, run)

    assert published.commit_sha != cloud_sha
    assert git(Path(workspace.path), "rev-parse", "HEAD^") == base
    assert git(Path(workspace.path), "log", "-1", "--format=%s") == commit_message_for(task)
    assert git(Path(workspace.path), "rev-parse", "HEAD^{tree}") == run.validated_revision
    assert git(remote, "rev-parse", f"refs/heads/{BRANCH}") == published.commit_sha


@pytest.mark.parametrize("invalid", [b"", b"garbage"])
def test_malformed_bundle_is_refused(tmp_path: Path, invalid: bytes) -> None:
    provider, workspace, _, base = scenario(tmp_path)
    with pytest.raises(CloudRevisionError):
        provider.materialize_bundle(workspace, RemoteRevision(base, BRANCH, REPO), invalid, base)
    assert git(Path(workspace.path), "rev-parse", "HEAD") == base


def test_claimed_sha_mismatch_is_refused(tmp_path: Path) -> None:
    provider, workspace, _, base = scenario(tmp_path)
    data, _ = provider.prepare_input(workspace)
    bundle, _ = result_bundle(tmp_path, data)
    with pytest.raises(CloudRevisionError):
        provider.materialize_bundle(workspace, RemoteRevision("0" * 40, BRANCH, REPO), bundle, base)


def test_wrong_branch_is_refused(tmp_path: Path) -> None:
    provider, workspace, _, base = scenario(tmp_path)
    data, _ = provider.prepare_input(workspace)
    bundle, sha = result_bundle(tmp_path, data)
    with pytest.raises(CloudRevisionError):
        provider.materialize_bundle(
            workspace, RemoteRevision(sha, "factory/other", REPO), bundle, base
        )
    assert git(Path(workspace.path), "rev-parse", "HEAD") == base


def test_rewritten_result_is_refused(tmp_path: Path) -> None:
    provider, workspace, source, base = scenario(tmp_path)
    data, _ = provider.prepare_input(workspace)
    input_bundle = tmp_path / "input.bundle"
    input_bundle.write_bytes(data)
    remote = tmp_path / "sandbox"
    git(tmp_path, "clone", "--branch", BRANCH, str(input_bundle), str(remote))
    git(remote, "checkout", "--orphan", "rewritten")
    git(remote, "rm", "-rf", ".")
    (remote / "replacement").write_text("unrelated\n")
    git(remote, "add", "-A")
    git(remote, "commit", "-m", "rewrite")
    git(remote, "branch", "-D", BRANCH)
    git(remote, "branch", "-m", BRANCH)
    sha = git(remote, "rev-parse", "HEAD")
    result = tmp_path / "rewrite.bundle"
    git(remote, "bundle", "create", str(result), f"refs/heads/{BRANCH}")

    with pytest.raises(CloudRevisionError):
        provider.materialize_bundle(
            workspace, RemoteRevision(sha, BRANCH, REPO), result.read_bytes(), base
        )
    assert git(Path(workspace.path), "rev-parse", "HEAD") == base
    assert git(Path(workspace.path), "status", "--porcelain") == ""
    assert git(source, "for-each-ref", "--format=%(refname)", "refs/factory-cloud") == ""


def test_dirty_local_base_is_refused(tmp_path: Path) -> None:
    provider, workspace, _, _ = scenario(tmp_path)
    (Path(workspace.path) / "dirty").write_text("x")
    with pytest.raises(CloudRevisionError):
        provider.prepare_input(workspace)


def test_private_origin_is_never_changed_or_fetched(tmp_path: Path) -> None:
    provider, workspace, source, base = scenario(tmp_path)
    private_url = "https://github.com/owner/private.git"
    git(source, "remote", "add", "origin", private_url)
    input_data, _ = provider.prepare_input(workspace)
    data, sha = result_bundle(tmp_path, input_data)
    provider.materialize_bundle(workspace, RemoteRevision(sha, BRANCH, REPO), data, base)
    assert git(source, "remote", "get-url", "origin") == private_url
    assert git(source, "for-each-ref", "--format=%(refname)", "refs/remotes") == ""


def test_cloud_base_ref_selects_local_revision_without_fetch(tmp_path: Path) -> None:
    _, _, source, original = scenario(tmp_path)
    (source / "README").write_text("later\n")
    git(source, "add", "README")
    git(source, "commit", "-m", "later")
    later = git(source, "rev-parse", "HEAD")
    git(source, "tag", "selected-base", original)
    private_url = "https://github.com/owner/private.git"
    git(source, "remote", "add", "origin", private_url)
    other = Workspace("other", REPO, "factory/task/other", str(tmp_path / "other"))
    task = FactoryTask("Task", REPO, TaskSource("github", REPO, 2))
    CloudWorkspaceProvisioner(str(source), base_ref="selected-base").prepare(task, other)
    assert git(Path(other.path), "rev-parse", "HEAD") == original
    assert git(source, "rev-parse", "HEAD") == later
    assert git(source, "remote", "get-url", "origin") == private_url
