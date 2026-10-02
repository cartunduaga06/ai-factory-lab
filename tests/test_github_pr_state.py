"""Read-only PR state lookup fails closed on ambiguous GitHub responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from factory.domain.errors import PullRequestHeadMismatchError
from factory.domain.models import PullRequest
from factory.domain.ports import PullRequestState
from factory.integrations.github.client import GitHubClient
from factory.integrations.github.pr_state import GitHubPullRequestStateSource


class Transport:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.urls: list[str] = []

    def get_json(self, url: str, headers: Mapping[str, str]) -> Any:  # noqa: ANN401
        self.urls.append(url)
        assert headers["Authorization"] == "Bearer token"
        return self.payload


OLD_HEAD = "39e9863d952d31899417d1b308f74ca5f9e1c485"
NEW_HEAD = "101527debd1d367faa86522050ef4b5596e374d7"


def _pr() -> PullRequest:
    return PullRequest(
        "example/target",
        "factory/one",
        "main",
        "title",
        number=38,
        commit_sha=OLD_HEAD,
    )


def _payload(state: str, merged_at: str | None = None) -> dict[str, object]:
    return {
        "number": 38,
        "state": state,
        "merged_at": merged_at,
        "head": {
            "sha": OLD_HEAD,
            "ref": "factory/one",
            "repo": {"full_name": "example/target"},
        },
        "base": {"ref": "main", "repo": {"full_name": "example/target"}},
    }


@pytest.mark.parametrize(
    ("state", "merged_at", "expected"),
    [
        ("open", None, PullRequestState.OPEN),
        ("closed", "2026-09-29T12:00:00Z", PullRequestState.MERGED),
        ("closed", None, PullRequestState.CLOSED),
    ],
)
def test_exact_pr_state(state: str, merged_at: str | None, expected: PullRequestState) -> None:
    transport = Transport(_payload(state, merged_at))
    source = GitHubPullRequestStateSource(
        GitHubClient("token", "https://api.github.com", transport)
    )
    assert source.state(_pr()) is expected
    assert transport.urls == ["https://api.github.com/repos/example/target/pulls/38"]


@pytest.mark.parametrize("field", ["number", "head", "base", "state", "merged_at"])
def test_mismatch_or_ambiguity_never_transitions(field: str) -> None:
    payload = _payload("closed", None)
    payload[field] = {"number": 39, "head": {}, "base": {}, "state": "draft", "merged_at": 42}[
        field
    ]
    source = GitHubPullRequestStateSource(
        GitHubClient("token", "https://api.github.com", Transport(payload))
    )
    with pytest.raises(ValueError):
        source.state(_pr())


def test_provider_head_change_requires_revalidation() -> None:
    payload = _payload("open")
    head = payload["head"]
    assert isinstance(head, dict)
    head["sha"] = NEW_HEAD
    source = GitHubPullRequestStateSource(
        GitHubClient("token", "https://api.github.com", Transport(payload))
    )

    with pytest.raises(PullRequestHeadMismatchError):
        source.state(_pr())


def test_missing_persisted_or_provider_head_requires_revalidation() -> None:
    payload = _payload("open")
    head = payload["head"]
    assert isinstance(head, dict)
    head.pop("sha")
    source = GitHubPullRequestStateSource(
        GitHubClient("token", "https://api.github.com", Transport(payload))
    )
    with pytest.raises(PullRequestHeadMismatchError):
        source.state(_pr())

    pr_without_revision = PullRequest("example/target", "factory/one", "main", "title", number=38)
    source = GitHubPullRequestStateSource(
        GitHubClient("token", "https://api.github.com", Transport(_payload("open")))
    )
    with pytest.raises(PullRequestHeadMismatchError):
        source.state(pr_without_revision)
