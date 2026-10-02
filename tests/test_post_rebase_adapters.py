"""Concrete read-only adapters bind revalidation evidence to one exact commit."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from factory.domain.errors import PullRequestHeadMismatchError, WorkspaceRevisionError
from factory.domain.models import PullRequest, Workspace
from factory.domain.revalidation import ExactHeadCiStatus
from factory.integrations.github.revalidation import GitHubPostRebaseEvidenceSource
from factory.integrations.workspace.revalidation import GitWorkspaceRevalidationInspector

OLD_HEAD = "39e9863d952d31899417d1b308f74ca5f9e1c485"
NEW_HEAD = "101527debd1d367faa86522050ef4b5596e374d7"


class Client:
    def __init__(self, checks: tuple[str, str] = ("success", "success")) -> None:
        self.checks = checks
        self.paths: list[str] = []

    def get(self, path: str, params: dict[str, int] | None = None) -> Any:  # noqa: ANN401
        del params
        self.paths.append(path)
        if path.endswith("/pulls/116"):
            return {
                "number": 116,
                "state": "open",
                "merged_at": None,
                "head": {
                    "sha": NEW_HEAD,
                    "ref": "factory/task/workspace",
                    "repo": {"full_name": "example/target"},
                },
                "base": {"ref": "main", "repo": {"full_name": "example/target"}},
            }
        if path.endswith(f"/commits/{NEW_HEAD}/status"):
            return {"statuses": []}
        if path.endswith(f"/commits/{NEW_HEAD}/check-runs"):
            rows = []
            for name, result in zip(("ci-3.11", "ci-3.12"), self.checks, strict=True):
                rows.append(
                    {
                        "name": name,
                        "status": "in_progress" if result == "pending" else "completed",
                        "conclusion": None if result == "pending" else result,
                    }
                )
            return {"total_count": len(rows), "check_runs": rows}
        raise AssertionError(path)


def _pr() -> PullRequest:
    return PullRequest(
        "example/target",
        "factory/task/workspace",
        "main",
        "title",
        number=116,
        task_id="task",
        run_id="run",
        commit_sha=OLD_HEAD,
    )


@pytest.mark.parametrize(
    ("checks", "expected"),
    [
        (("success", "success"), ExactHeadCiStatus.PASSED),
        (("success", "pending"), ExactHeadCiStatus.PENDING),
        (("success", "failure"), ExactHeadCiStatus.FAILED),
    ],
)
def test_github_ci_is_bound_to_new_exact_head(
    checks: tuple[str, str], expected: ExactHeadCiStatus
) -> None:
    client = Client(checks)
    source = GitHubPostRebaseEvidenceSource(client)  # type: ignore[arg-type]
    assert source.current_head(_pr()) == NEW_HEAD

    evidence = source.exact_ci(_pr(), NEW_HEAD, ("ci-3.11", "ci-3.12"), required=True)

    assert evidence.sha == NEW_HEAD
    assert evidence.status is expected
    assert evidence.checks == ("ci-3.11", "ci-3.12")
    assert f"/repos/example/target/commits/{NEW_HEAD}/check-runs" in client.paths
    assert not any(OLD_HEAD in path for path in client.paths)


def test_github_head_change_during_revalidation_fails_before_ci_lookup() -> None:
    client = Client()
    source = GitHubPostRebaseEvidenceSource(client)  # type: ignore[arg-type]

    with pytest.raises(PullRequestHeadMismatchError):
        source.exact_ci(_pr(), OLD_HEAD, ("ci-3.11", "ci-3.12"), required=True)

    assert not any("/check-runs" in path for path in client.paths)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_workspace_snapshot_requires_clean_exact_commit(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "file.txt").write_text("one\n")
    _git(root, "add", "file.txt")
    _git(
        root,
        "-c",
        "user.name=test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-q",
        "-m",
        "initial",
    )
    workspace = Workspace(
        workspace_id="ws",
        repository_slug="example/target",
        branch="factory/task/workspace",
        path=str(root),
    )
    inspector = GitWorkspaceRevalidationInspector()

    snapshot = inspector.snapshot(workspace)

    assert snapshot.head_sha == _git(root, "rev-parse", "HEAD")
    assert snapshot.tree_sha == _git(root, "rev-parse", "HEAD^{tree}")
    (root / "file.txt").write_text("dirty\n")
    with pytest.raises(WorkspaceRevisionError):
        inspector.snapshot(workspace)
