"""Backlog materialization and provider boundaries use only in-memory transports."""

from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from factory.domain.backlog import MaterializedIssue, WorkItem
from factory.domain.enums import TaskStatus
from factory.domain.models import FactoryTask, TaskSource
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.backlog_sqlite import SqliteBacklogLinkRepository
from factory.infrastructure.persistence.schema import CREATE_TASKS
from factory.infrastructure.persistence.sqlite import SqliteTaskRepository
from factory.integrations.github.backlog_issues import GitHubBacklogIssueSink
from factory.integrations.github.write_client import GitHubWriteClient
from factory.integrations.trello.backlog import TrelloBacklogError, TrelloBacklogSource
from factory.orchestration.backlog import BacklogMaterializationService

CARD = "a" * 24
ITEM = WorkItem("trello", CARD, "Implement feature", "body", "example/control", True, True)
ISSUE = MaterializedIssue("example/control", 71, "https://github.com/example/control/issues/71")


class Source:
    def __init__(self, item: WorkItem = ITEM) -> None:
        self.item = item
        self.on_get: WorkItem | None = None

    def list_items(self) -> list[WorkItem]:
        return [self.item]

    def get_item(self, external_id: str) -> WorkItem:
        assert external_id == CARD
        return self.on_get or self.item


class Sink:
    def __init__(self) -> None:
        self.remote: MaterializedIssue | None = None
        self.posts = 0
        self.fail_after_create = False

    def find_issue(self, item: WorkItem) -> MaterializedIssue | None:
        return self.remote

    def create_issue(self, item: WorkItem) -> MaterializedIssue:
        self.posts += 1
        self.remote = ISSUE
        if self.fail_after_create:
            raise RuntimeError("secret from remote")
        return ISSUE


def _service(path: Path, source: Source, sink: Sink) -> BacklogMaterializationService:
    links = SqliteBacklogLinkRepository(str(path))
    links.initialize()
    return BacklogMaterializationService(source, sink, links)


def test_one_issue_and_durable_link_across_restart(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    source = Source()
    sink = Sink()
    assert _service(path, source, sink).reconcile().created == 1
    assert _service(path, source, sink).reconcile().existing == 1
    assert sink.posts == 1
    links = SqliteBacklogLinkRepository(str(path))
    assert links.get_issue("trello", CARD) == ISSUE

    tasks = SqliteTaskRepository(str(path))
    tasks.save(
        FactoryTask(
            title="Implement feature",
            target_repository="example/product",
            source=TaskSource("github", "example/control", 71),
            status=TaskStatus.READY,
        )
    )
    assert [
        event.name for event in SqliteAuditEventStore(str(path)).for_work_item("trello", CARD)
    ] == [
        "IssueMaterialized",
        "WorkItemReady",
    ]


def test_ineligible_dependency_and_changed_eligibility_never_post(tmp_path: Path) -> None:
    for item in (replace(ITEM, eligible=False), replace(ITEM, dependencies_satisfied=False)):
        source, sink = Source(item), Sink()
        assert (
            _service(
                tmp_path / (str(item.eligible) + str(item.dependencies_satisfied)), source, sink
            )
            .reconcile()
            .ineligible
            == 1
        )
        assert sink.posts == 0
    source, sink = Source(), Sink()
    source.on_get = replace(ITEM, eligible=False)
    assert _service(tmp_path / "changed.db", source, sink).reconcile().ineligible == 1
    assert sink.posts == 0


def test_ambiguous_post_recovers_without_duplicate(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    source, sink = Source(), Sink()
    sink.fail_after_create = True
    with pytest.raises(RuntimeError):
        _service(path, source, sink).reconcile()
    assert _service(path, source, sink).reconcile().existing == 1
    assert sink.posts == 1
    assert SqliteBacklogLinkRepository(str(path)).get_issue("trello", CARD) == ISSUE


def test_uncertain_without_remote_issue_never_reposts(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    source, sink = Source(), Sink()
    links = SqliteBacklogLinkRepository(str(path))
    links.initialize()
    links.reserve(ITEM)
    assert links.begin_write(ITEM)
    assert _service(path, source, sink).reconcile().uncertain == 1
    assert sink.posts == 0


def test_schema_adds_links_without_changing_existing_tasks(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(CREATE_TASKS)
        conn.execute(
            "INSERT INTO tasks (task_id, title, target_repository, status, created_at, updated_at) "
            "VALUES ('old', 'Old task', 'example/product', 'READY', '2026-01-01', '2026-01-01')"
        )
    links = SqliteBacklogLinkRepository(str(path))
    links.initialize()
    links.initialize()
    assert SqliteTaskRepository(str(path)).get("old") is not None
    assert links.reserve(ITEM)
    assert not links.reserve(ITEM)


def test_conflicting_card_or_issue_identity_is_refused(tmp_path: Path) -> None:
    links = SqliteBacklogLinkRepository(str(tmp_path / "factory.db"))
    links.initialize()
    links.reserve(ITEM)
    links.complete(ITEM, ISSUE)
    with pytest.raises(ValueError):
        links.reserve(replace(ITEM, target_repository="example/other"))
    other = replace(ITEM, external_id="b" * 24)
    links.reserve(other)
    with pytest.raises(ValueError):
        links.complete(other, ISSUE)


class TrelloFake:
    def __init__(self, card: dict[str, Any], checklists: Any) -> None:  # noqa: ANN401
        self.card = card
        self.checklists = checklists
        self.requests: list[tuple[str, dict[str, str]]] = []

    def get_json(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ANN401
        self.requests.append((url, headers))
        if "/checklists" in url:
            return self.checklists
        if "/lists/" in url:
            return [{"id": CARD}]
        return self.card


def test_trello_source_checks_sprint_ready_dependencies_and_hides_token() -> None:
    card = {
        "id": CARD,
        "idList": "sprint123",
        "idLabels": ["ready123"],
        "closed": False,
        "name": "Feature",
        "desc": "Description",
    }
    fake = TrelloFake(card, [{"name": "Dependencies", "checkItems": [{"state": "complete"}]}])
    source = TrelloBacklogSource(
        "key_secret", "token_secret", "sprint123", "ready123", "example/control", fake
    )
    assert source.list_items()[0].dependencies_satisfied
    assert source.get_item(CARD).eligible
    assert all("token_secret" not in url for url, _ in fake.requests)
    assert "token_secret" not in repr(source)
    fake.checklists[0]["checkItems"][0]["state"] = "incomplete"
    assert not source.get_item(CARD).dependencies_satisfied
    fake.checklists = [{"name": "Dependencies", "checkItems": [{}]}]
    with pytest.raises(TrelloBacklogError):
        source.get_item(CARD)


def test_trello_provider_failure_discards_secret_text() -> None:
    class FailingTransport:
        def get_json(self, url: str, headers: dict[str, str]) -> Any:  # noqa: ANN401
            raise RuntimeError("token_secret in provider response")

    source = TrelloBacklogSource(
        "key_secret",
        "token_secret",
        "sprint123",
        "ready123",
        "example/control",
        FailingTransport(),
    )
    with pytest.raises(TrelloBacklogError) as captured:
        source.get_item(CARD)
    assert "token_secret" not in str(captured.value)
    assert captured.value.__context__ is None


class GitHubFake:
    def __init__(self) -> None:
        self.issues: list[dict[str, Any]] = []
        self.posts = 0
        self.requests: list[tuple[str, str, dict[str, str], Any]] = []

    def request_json(self, method: str, url: str, headers: dict[str, str], body: Any) -> Any:  # noqa: ANN401
        self.requests.append((method, url, headers, body))
        if method == "GET":
            return self.issues
        self.posts += 1
        issue = {
            "number": 71,
            "html_url": ISSUE.url,
            "body": body["body"],
            "labels": body["labels"],
        }
        self.issues.append(issue)
        return issue


def test_github_sink_marks_and_recovers_factory_ready_issue() -> None:
    fake = GitHubFake()
    sink = GitHubBacklogIssueSink(GitHubWriteClient("secret", "https://api.github.com", fake))
    assert sink.find_issue(ITEM) is None
    assert sink.create_issue(ITEM) == ISSUE
    assert sink.find_issue(ITEM) == ISSUE
    assert fake.posts == 1
    assert fake.issues[0]["labels"] == ["factory-ready"]
    assert "factory-work-item:" in fake.issues[0]["body"]
    assert all("secret" not in url for _, url, _, _ in fake.requests)
