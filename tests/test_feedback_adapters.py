"""Provider feedback writes are exact and retry safe."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus, TaskStatus
from factory.domain.feedback import FeedbackIdentity
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PullRequest,
    QualityGate,
    QualityGateSpec,
    TaskSource,
    Workspace,
)
from factory.domain.projects import ProjectProfile, ProjectRegistry
from factory.integrations.github.issue_completion import GitHubIssueCompletionSink
from factory.integrations.github.owner_reports import GitHubOwnerReportSink
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
        "factory/carda",
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
        self.comments: list[dict[str, Any]] = []
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
            return [dict(comment, user={"login": "factory"}) for comment in self.comments]
        if method == "GET":
            result = dict(self.issue)
            if result.get("state") == "closed" and result.get("state_reason") is None:
                result["state_reason"] = "not_planned"
            return result
        self.writes += 1
        if method == "POST":
            assert body is not None
            self.comments.append({"body": str(body["body"]), "id": len(self.comments) + 1})
            return self.comments[-1]
        if method == "PATCH" and "/comments/" in url:
            assert body is not None
            comment_id = int(url.rsplit("/", 1)[-1])
            for comment in self.comments:
                if comment["id"] == comment_id:
                    comment["body"] = str(body["body"])
                    return comment
            raise AssertionError("missing comment")
        assert method == "PATCH" and body is not None
        if "labels" in body:
            self.issue["labels"] = [{"name": label} for label in body["labels"]]
        else:
            self.issue.update(body)
        return dict(self.issue)


def test_owner_report_is_idempotent_sanitized_and_never_false_passes() -> None:
    transport = GitHubTransport()
    sink = GitHubOwnerReportSink(GitHubWriteClient("secret", "https://api.github.com", transport))
    task = FactoryTask(
        title="Owner report ghp_secret",
        target_repository="example/project-a",
        source=TaskSource("github", "example/project-a", 7),
        task_id="task-safe",
    )
    from factory.infrastructure.persistence.audit import AuditEvent

    event = AuditEvent(
        1,
        "IssueMaterialized",
        "task-safe",
        "task-safe",
        None,
        None,
        None,
        "safe",
        "task",
        "task-safe",
        1,
        "2026-01-01T00:00:00Z",
        "github",
        7,
        evidence={"private": "token=secret"},
    )
    sink.publish(task, (), (), (event,), initial=True)
    assert len(transport.comments) == 1
    assert "Final status: **BLOCKED**" in transport.comments[0]["body"]
    assert "ghp_secret" not in transport.comments[0]["body"]
    assert "token=secret" not in transport.comments[0]["body"]
    sink.publish(task, (), (), (event,), initial=True)
    assert len(transport.comments) == 1
    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.CODEX,
        run_id="run-safe",
        status=RunStatus.SUCCEEDED,
        workspace=Workspace(repository_slug=task.target_repository, branch="factory/task-safe/run"),
        summary="service password=secret",
        gates=(QualityGate("tests", QualityGateStatus.PASSED, "stdout: secret"),),
    )
    sink.publish(task, (run,), (), (event,), initial=False)
    report = transport.comments[0]["body"]
    assert "Final status: **BLOCKED**" in report
    assert "stdout" not in report and "password" not in report and "secret" not in report
    task.status = TaskStatus.DONE
    merged_pr = PullRequest(
        repository_slug="example/project-a",
        head_branch="factory/task-safe/run",
        base_branch="main",
        title="Change",
        number=17,
        url="https://github.com/example/project-a/pull/17",
        task_id=task.task_id,
        run_id=run.run_id,
        merged=True,
        commit_sha="a" * 40,
    )
    delivery_event = AuditEvent(
        2,
        "DeliveryReconciled",
        "task-safe",
        "task-safe",
        "run-safe",
        None,
        None,
        "safe",
        "task",
        "task-safe",
        2,
        "2026-01-01T00:01:00Z",
        "github",
        7,
    )
    sink.publish(task, (run,), (merged_pr,), (event, delivery_event), initial=False)
    assert "Final status: **PASS**" in transport.comments[0]["body"]


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


def test_issue_resolution_state_and_reason_are_exact_and_idempotent() -> None:
    transport = GitHubTransport()
    sink = GitHubIssueCompletionSink(
        GitHubWriteClient("secret", "https://api.github.com", transport)
    )
    assert sink.state("example/project-a", 7) == ("open", None)
    sink.close(_identity(), "not_planned")
    assert sink.state("example/project-a", 7) == ("closed", "not_planned")
    assert len(transport.comments) == 1
    writes = transport.writes
    sink.close(_identity(), "not_planned")
    assert transport.writes == writes


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


def test_trello_feedback_replaces_owned_block_after_human_description_edit() -> None:
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
    original = "project_id: project-a\nOriginal"
    transport = TrelloTransport(original)
    sink = TrelloWorkItemFeedbackSink("key", "token", registry, transport)
    sink.sync(_identity(), "WAITING_HUMAN")

    # A user appending a note after the feedback marker must not make the
    # Factory block look like source routing data or cause another block.
    transport.card["desc"] += "\n\nHuman note"
    sink.sync(_identity(), "MERGED")

    description = transport.card["desc"]
    assert source_description(description) == original + "\n\nHuman note"
    assert description.count("<!-- factory-feedback:start -->") == 1
    assert "Factory: MERGED" in description
    assert transport.writes == 2
    sink.sync(_identity(), "MERGED")
    assert transport.writes == 2


def test_trello_feedback_materializes_missing_project_marker_once() -> None:
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
    transport = TrelloTransport("Historical card description")
    sink = TrelloWorkItemFeedbackSink("key", "token", registry, transport)

    sink.sync(_identity(), "WAITING_HUMAN")
    expected_source = "project_id: project-a\nHistorical card description"
    assert source_description(transport.card["desc"]) == expected_source
    assert transport.card["desc"].count("project_id:") == 1
    assert transport.writes == 1

    sink.sync(_identity(), "WAITING_HUMAN")
    assert source_description(transport.card["desc"]) == expected_source
    assert transport.card["desc"].count("<!-- factory-feedback:start -->") == 1
    assert transport.writes == 1


@pytest.mark.parametrize(
    "description",
    (
        "project_id: project-b\nConflicting project",
        "project_id: project-a\nOriginal\nproject_id: project-a",
    ),
)
def test_trello_feedback_rejects_conflicting_or_duplicate_project_markers(
    description: str,
) -> None:
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
    transport = TrelloTransport(description)
    sink = TrelloWorkItemFeedbackSink("key", "token", registry, transport)
    with pytest.raises(TrelloFeedbackError, match="project identity mismatch"):
        sink.sync(_identity(), "WAITING_HUMAN")
    assert transport.writes == 0


def test_trello_feedback_fails_closed_on_ambiguous_owned_markers() -> None:
    description = (
        "project_id: project-a\nOriginal\n\n"
        "<!-- factory-feedback:start -->\nFactory: WAITING_HUMAN\n"
        "<!-- factory-feedback:end -->\n"
        "<!-- factory-feedback:start -->\nFactory: MERGED\n"
        "<!-- factory-feedback:end -->"
    )
    with pytest.raises(TrelloFeedbackError, match="invalid Trello feedback markers"):
        source_description(description)


class DeliveryClient:
    def __init__(self, *, missing_check: bool = False, stale_check: bool = False) -> None:
        self.paths: list[str] = []
        self.missing_check = missing_check
        self.stale_check = stale_check

    def get(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:  # noqa: ANN401
        self.paths.append(path)
        if path.endswith("/pulls/17"):
            return {
                "number": 17,
                "state": "closed",
                "merged": True,
                "merged_at": "2026-09-30T22:33:47Z",
                "merge_commit_sha": "b" * 40,
                "head": {
                    "sha": "a" * 40,
                    "ref": "factory/carda",
                    "repo": {"full_name": "example/project-a"},
                },
                "base": {"ref": "main", "repo": {"full_name": "example/project-a"}},
            }
        if path.endswith("/commits/" + "b" * 40):
            return {"sha": "b" * 40}
        if path.endswith("/commits/" + "a" * 40 + "/status"):
            return {"statuses": []}
        if path.endswith("/commits/" + "a" * 40 + "/check-runs"):
            rows = [
                {
                    "id": 101,
                    "name": "ci-3.11",
                    "head_sha": "c" * 40 if self.stale_check else "a" * 40,
                    "status": "completed",
                    "conclusion": "success",
                },
                {
                    "id": 102,
                    "name": "ci-3.12",
                    "head_sha": "a" * 40,
                    "status": "completed",
                    "conclusion": "success",
                },
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


def test_delivery_can_use_successful_local_gates_without_provider_ci_queries() -> None:
    from factory.integrations.github.delivery import GitHubDeliveryEvidenceSource

    registry = ProjectRegistry(
        (
            ProjectProfile(
                "project-a",
                "example/project-a",
                "/tmp/project-a",
                "main",
                (QualityGateSpec("tests", ("pytest",)),),
                provider_ci_required=False,
            ),
        )
    )
    client = DeliveryClient()
    evidence = GitHubDeliveryEvidenceSource(client, registry).evidence(_identity())  # type: ignore[arg-type]

    assert evidence.complete
    assert not any(
        marker in path
        for path in client.paths
        for marker in ("/protection/", "/status", "/check-runs")
    )


def test_delivery_fails_closed_when_configured_ci_check_is_missing() -> None:
    from factory.integrations.github.delivery import GitHubDeliveryEvidenceSource

    evidence = GitHubDeliveryEvidenceSource(  # type: ignore[arg-type]
        DeliveryClient(missing_check=True), _delivery_registry()
    ).evidence(_identity())
    assert not evidence.required_ci_passed
    assert not evidence.complete


def test_delivery_rejects_successful_check_run_for_stale_head_sha() -> None:
    from factory.integrations.github.delivery import GitHubDeliveryEvidenceSource

    evidence = GitHubDeliveryEvidenceSource(  # type: ignore[arg-type]
        DeliveryClient(stale_check=True), _delivery_registry()
    ).evidence(_identity())
    assert not evidence.required_ci_passed
    assert not evidence.complete


def test_delivery_fails_closed_on_provider_head_branch_mismatch() -> None:
    from dataclasses import replace

    from factory.integrations.github.delivery import GitHubDeliveryEvidenceSource

    evidence = GitHubDeliveryEvidenceSource(  # type: ignore[arg-type]
        DeliveryClient(), _delivery_registry()
    ).evidence(replace(_identity(), branch="factory/other"))
    assert not evidence.merged
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
