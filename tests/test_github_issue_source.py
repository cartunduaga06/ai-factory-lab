"""Tests for the read-only GitHub issue source.

No test touches the network: a fake transport records requests and returns
canned payloads, so the adapter's filtering, pagination and error handling are
exercised deterministically.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from factory.domain.enums import RepositoryRole
from factory.domain.models import Repository, TaskSource
from factory.integrations.github.client import (
    GitHubAuthError,
    GitHubClient,
    GitHubRequestError,
)
from factory.integrations.github.issues import GitHubIssueSource

REPO = Repository(slug="cartunduaga06/ai-factory-lab", role=RepositoryRole.CONTROL_PLANE)

#: A token-shaped string that must never appear in output.
TOKEN = "ghp_super_secret_token_value"


class FakeTransport:
    """In-memory transport: returns queued responses and records calls."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get_json(self, url: str, headers: Mapping[str, str]) -> Any:  # noqa: ANN401
        self.calls.append((url, dict(headers)))
        if not self._responses:
            return []
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _issue(
    number: int,
    *,
    labels: list[str] | None = None,
    state: str = "open",
    is_pull_request: bool = False,
    title: str | None = None,
    body: str = "details",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "number": number,
        "title": title if title is not None else f"Issue {number}",
        "body": body,
        "state": state,
        "labels": [{"name": name} for name in (labels or [])],
    }
    if is_pull_request:
        payload["pull_request"] = {"url": "https://api.github.com/prs/1"}
    return payload


def _source(transport: FakeTransport, per_page: int = 100) -> GitHubIssueSource:
    client = GitHubClient(token=TOKEN, api_url="https://api.github.com", transport=transport)
    return GitHubIssueSource(client, per_page=per_page)


# -- mapping ---------------------------------------------------------------


def test_eligible_issue_is_mapped_into_factory_task() -> None:
    transport = FakeTransport([[_issue(10, labels=["factory-ready", "backend"])]])
    tasks = _source(transport).list_open_tasks(REPO)

    assert len(tasks) == 1
    task = tasks[0]
    assert task.title == "Issue 10"
    assert task.body == "details"
    assert task.source == TaskSource("github", "cartunduaga06/ai-factory-lab", 10)
    assert task.external_ref == "cartunduaga06/ai-factory-lab#10"
    assert task.status.value == "DISCOVERED"


def test_additional_labels_are_preserved() -> None:
    transport = FakeTransport([[_issue(1, labels=["factory-ready", "backend", "urgent"])]])
    tasks = _source(transport).list_open_tasks(REPO)
    assert set(tasks[0].labels) == {"factory-ready", "backend", "urgent"}


def test_target_repository_override_is_used_when_given() -> None:
    transport = FakeTransport([[_issue(1, labels=["factory-ready"])]])
    client = GitHubClient(token=TOKEN, api_url="https://api.github.com", transport=transport)
    source = GitHubIssueSource(client, target_repository="cartunduaga06/finanza-ia")
    tasks = source.list_open_tasks(REPO)
    assert tasks[0].target_repository == "cartunduaga06/finanza-ia"
    # Source identity still points at where the issue actually lives.
    assert tasks[0].source is not None
    assert tasks[0].source.repository_slug == "cartunduaga06/ai-factory-lab"


def test_string_labels_are_supported() -> None:
    payload = _issue(1)
    payload["labels"] = ["factory-ready"]
    tasks = _source(FakeTransport([[payload]])).list_open_tasks(REPO)
    assert tasks[0].labels == ("factory-ready",)


# -- eligibility filter ----------------------------------------------------


def test_issue_without_factory_ready_label_is_skipped() -> None:
    transport = FakeTransport([[_issue(11, labels=["backend"])]])
    assert _source(transport).list_open_tasks(REPO) == []


def test_issue_with_no_labels_is_skipped() -> None:
    transport = FakeTransport([[_issue(11)]])
    assert _source(transport).list_open_tasks(REPO) == []


def test_pull_request_is_excluded_even_when_labelled() -> None:
    transport = FakeTransport([[_issue(12, labels=["factory-ready"], is_pull_request=True)]])
    assert _source(transport).list_open_tasks(REPO) == []


def test_closed_issue_is_excluded() -> None:
    transport = FakeTransport([[_issue(13, labels=["factory-ready"], state="closed")]])
    assert _source(transport).list_open_tasks(REPO) == []


def test_mixed_page_keeps_only_eligible_items() -> None:
    transport = FakeTransport(
        [
            [
                _issue(10, labels=["factory-ready", "backend"]),
                _issue(11, labels=["backend"]),
                _issue(12, labels=["factory-ready"], is_pull_request=True),
            ]
        ]
    )
    tasks = _source(transport).list_open_tasks(REPO)
    assert [task.source.issue_number for task in tasks if task.source] == [10]


# -- pagination ------------------------------------------------------------


def test_pagination_walks_multiple_pages() -> None:
    page_one = [_issue(n, labels=["factory-ready"]) for n in range(1, 3)]
    page_two = [_issue(n, labels=["factory-ready"]) for n in range(3, 5)]
    transport = FakeTransport([page_one, page_two, []])
    tasks = _source(transport, per_page=2).list_open_tasks(REPO)

    assert [task.source.issue_number for task in tasks if task.source] == [1, 2, 3, 4]
    pages = [call[0] for call in transport.calls]
    assert "page=1" in pages[0]
    assert "page=2" in pages[1]


def test_short_page_stops_pagination() -> None:
    page_one = [_issue(1, labels=["factory-ready"])]
    transport = FakeTransport([page_one])
    tasks = _source(transport, per_page=100).list_open_tasks(REPO)
    assert len(tasks) == 1
    assert len(transport.calls) == 1


def test_state_open_is_requested() -> None:
    transport = FakeTransport([[]])
    _source(transport).list_open_tasks(REPO)
    assert "state=open" in transport.calls[0][0]


# -- malformed payloads ----------------------------------------------------


def test_malformed_item_does_not_abort_the_page() -> None:
    payload = [
        {"number": "not-an-int", "title": "bad", "labels": ["factory-ready"]},
        _issue(2, labels=["factory-ready"]),
    ]
    tasks = _source(FakeTransport([payload])).list_open_tasks(REPO)
    assert [task.source.issue_number for task in tasks if task.source] == [2]


def test_item_missing_title_is_skipped() -> None:
    payload = [{"number": 9, "title": "", "labels": ["factory-ready"]}]
    assert _source(FakeTransport([payload])).list_open_tasks(REPO) == []


def test_non_mapping_items_are_ignored() -> None:
    assert _source(FakeTransport([["nonsense", 42]])).list_open_tasks(REPO) == []


def test_non_list_payload_raises_request_error() -> None:
    with pytest.raises(GitHubRequestError):
        _source(FakeTransport([{"unexpected": "object"}])).list_open_tasks(REPO)


# -- get_task --------------------------------------------------------------


def test_get_task_returns_mapped_issue() -> None:
    transport = FakeTransport([_issue(5, labels=["factory-ready"])])
    task = _source(transport).get_task(
        REPO, TaskSource("github", "cartunduaga06/ai-factory-lab", 5)
    )
    assert task.source == TaskSource("github", "cartunduaga06/ai-factory-lab", 5)
    assert "/issues/5" in transport.calls[0][0]


def test_get_task_rejects_pull_request() -> None:
    transport = FakeTransport([_issue(5, labels=["factory-ready"], is_pull_request=True)])
    with pytest.raises(GitHubRequestError):
        _source(transport).get_task(REPO, TaskSource("github", "cartunduaga06/ai-factory-lab", 5))


# -- failure behaviour -----------------------------------------------------


def test_api_failure_propagates_as_request_error() -> None:
    transport = FakeTransport([GitHubRequestError(500, "server error")])
    with pytest.raises(GitHubRequestError):
        _source(transport).list_open_tasks(REPO)


def test_authentication_failure_propagates_as_auth_error() -> None:
    transport = FakeTransport([GitHubAuthError("GitHub authentication failed with status 401")])
    with pytest.raises(GitHubAuthError):
        _source(transport).list_open_tasks(REPO)


def test_token_is_sent_as_bearer_header() -> None:
    transport = FakeTransport([[]])
    _source(transport).list_open_tasks(REPO)
    headers = transport.calls[0][1]
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_token_never_appears_in_client_repr() -> None:
    client = GitHubClient(
        token=TOKEN, api_url="https://api.github.com", transport=FakeTransport([])
    )
    assert TOKEN not in repr(client)
    assert TOKEN not in repr(_source(FakeTransport([])))


def test_token_never_appears_in_error_messages() -> None:
    # Even when a failure is constructed, the message must not embed the token.
    error = GitHubAuthError("GitHub authentication failed with status 403")
    assert TOKEN not in str(error)
    assert TOKEN not in repr(error)
