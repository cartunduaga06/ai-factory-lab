"""Read-only PR state lookup fails closed on ambiguous GitHub responses."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

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


def _pr() -> PullRequest:
    return PullRequest("example/target", "factory/one", "main", "title", number=38)


def _payload(state: str, merged_at: str | None = None) -> dict[str, object]:
    return {
        "number": 38,
        "state": state,
        "merged_at": merged_at,
        "head": {"ref": "factory/one", "repo": {"full_name": "example/target"}},
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
