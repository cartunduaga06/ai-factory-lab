"""Provider feedback writes are exact and retry safe."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from factory.domain.feedback import FeedbackIdentity
from factory.domain.models import QualityGateSpec
from factory.domain.projects import ProjectProfile, ProjectRegistry
from factory.integrations.github.issue_completion import GitHubIssueCompletionSink
from factory.integrations.github.write_client import GitHubWriteClient
from factory.integrations.trello.feedback import (
    TrelloFeedbackError,
    TrelloWorkItemFeedbackSink,
    source_description,
)


def _identity(project: str = "project-a") -> FeedbackIdentity:
    return FeedbackIdentity(
        project,
        f"example/{project}",
        7,
        "task-1",
        "run-1",
        "workspace-1",
        "a" * 40,
        17,
        "sprint-one",
        "trello",
        "carda",
    )


class GitHubTransport:
    def __init__(self) -> None:
        self.issue: dict[str, Any] = {
            "number": 7,
            "html_url": "https://github.com/example/project-a/issues/7",
            "state": "open",
            "state_reason": None,
            "labels": [{"name": "factory-ready"}, {"name": "other"}],
        }
        self.comments: list[dict[str, str]] = []
        self.writes = 0

    def request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> Any:  # noqa: ANN401
        assert "/repos/example/project-a/issues/7" in url
        if method == "GET" and "/comments" in url:
            return list(self.comments)
        if method == "GET":
            return dict(self.issue)
        self.writes += 1
        assert body is not None
        if method == "POST":
            self.comments.append({"body": str(body["body"])})
            return self.comments[-1]
        assert method == "PATCH"
        if "labels" in body:
            self.issue["labels"] = [{"name": label} for label in body["labels"]]
        else:
            self.issue.update(body)
        return dict(self.issue)


def test_issue_completion_retries_without_duplicate_writes() -> None:
    transport = GitHubTransport()
    sink = GitHubIssueCompletionSink(
        GitHubWriteClient("secret", "https://api.github.com", transport)
    )
    sink.complete(_identity())
    assert transport.writes == 3
    assert transport.issue["state_reason"] == "completed"
    assert transport.issue["labels"] == [{"name": "other"}]
    sink.complete(_identity())
    assert transport.writes == 3
    assert len(transport.comments) == 1


class TrelloTransport:
    def __init__(self, description: str) -> None:
        self.card: dict[str, Any] = {
            "id": "carda",
            "desc": description,
            "dueComplete": False,
            "idList": "backlog",
        }
        self.writes = 0

    def request_json(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> Any:  # noqa: ANN401
        assert "/cards/carda" in url
        if method == "GET":
            return dict(self.card)
        assert method == "PUT" and body is not None
        self.writes += 1
        self.card.update(json.loads(body))
        return dict(self.card)


def test_trello_feedback_preserves_source_snapshot_and_project() -> None:
    registry = ProjectRegistry(
        (
            ProjectProfile(
                "project-a",
                "example/project-a",
                "/tmp/project-a",
                "main",
                (QualityGateSpec("tests", ("pytest",)),),
            ),
        )
    )
    transport = TrelloTransport("project_id: project-a\nOriginal")
    sink = TrelloWorkItemFeedbackSink("key", "token", registry, transport)
    sink.sync(_identity(), "DONE")
    assert transport.card["dueComplete"] is True
    assert source_description(transport.card["desc"]) == "project_id: project-a\nOriginal"
    sink.sync(_identity(), "DONE")
    assert transport.writes == 1
    with pytest.raises(TrelloFeedbackError):
        sink.sync(_identity("project-b"), "DONE")
    assert transport.writes == 1


class DeliveryClient:
    def __init__(self, *, missing_check: bool = False) -> None:
        self.paths: list[str] = []
        self.missing_check = missing_check

    def get(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:  # noqa: ANN401
        self.paths.append(path)
        if path.endswith("/pulls/17"):
            return {
                "number": 17,
                "state": "closed",
                "merged": True,
                "merged_at": "2026-09-30T22:33:47Z",
                "merge_commit_sha": "b" * 40,
                "head": {"sha": "a" * 40, "repo": {"full_name": "example/project-a"}},
                "base": {"ref": "main", "repo": {"full_name": "example/project-a"}},
            }
        if path.endswith("/commits/" + "b" * 40):
            return {"sha": "b" * 40}
        if path.endswith("/commits/" + "a" * 40 + "/status"):
            return {"statuses": []}
        if path.endswith("/commits/" + "a" * 40 + "/check-runs"):
            rows = [
                {"name": "ci-3.11", "status": "completed", "conclusion": "success"},
                {"name": "ci-3.12", "status": "completed", "conclusion": "success"},
            ]
            if self.missing_check:
                rows = rows[:1]
            return {"total_count": len(rows), "check_runs": rows}
        if "/protection/" in path:
            raise AssertionError("branch protection must not be queried when checks are configured")
        raise AssertionError(path)


def _delivery_registry() -> ProjectRegistry:
    return ProjectRegistry(
        (
            ProjectProfile(
                "project-a",
                "example/project-a",
                "/tmp/project-a",
                "main",
                (QualityGateSpec("tests", ("pytest",)),),
                required_ci_checks=("ci-3.11", "ci-3.12"),
            ),
        )
    )


def test_delivery_uses_explicit_required_ci_checks_without_branch_protection() -> None:
    from factory.integrations.github.delivery import GitHubDeliveryEvidenceSource

    client = DeliveryClient()
    evidence = GitHubDeliveryEvidenceSource(client, _delivery_registry()).evidence(_identity())  # type: ignore[arg-type]
    assert evidence.complete
    assert not any("/protection/" in path for path in client.paths)


def test_delivery_fails_closed_when_configured_ci_check_is_missing() -> None:
    from factory.integrations.github.delivery import GitHubDeliveryEvidenceSource

    evidence = GitHubDeliveryEvidenceSource(  # type: ignore[arg-type]
        DeliveryClient(missing_check=True), _delivery_registry()
    ).evidence(_identity())
    assert not evidence.required_ci_passed
    assert not evidence.complete


def test_trello_done_moves_card_to_configured_done_list_idempotently() -> None:
    registry = ProjectRegistry(
        (
            ProjectProfile(
                "project-a",
                "example/project-a",
                "/tmp/project-a",
                "main",
                (QualityGateSpec("tests", ("pytest",)),),
            ),
        )
    )
    transport = TrelloTransport("project_id: project-a\nOriginal")
    sink = TrelloWorkItemFeedbackSink("key", "token", registry, transport, done_list_id="done123")
    sink.sync(_identity(), "DONE")
    assert transport.card["dueComplete"] is True
    assert transport.card["idList"] == "done123"
    assert transport.writes == 1
    sink.sync(_identity(), "DONE")
    assert transport.writes == 1
