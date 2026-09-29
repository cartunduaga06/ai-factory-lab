"""Read-only, identity-checked GitHub review state for persisted PRs."""

from __future__ import annotations

from collections.abc import Mapping

from factory.domain.models import PullRequest
from factory.domain.ports import PullRequestState, PullRequestStateSource
from factory.integrations.github.client import GitHubClient


class GitHubPullRequestStateSource(PullRequestStateSource):
    """Read exactly one PR; uncertain responses cannot advance a task."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def state(self, pull_request: PullRequest) -> PullRequestState:
        if pull_request.number is None or pull_request.number <= 0:
            raise ValueError("persisted pull request has no number")
        item = self._client.get(
            f"/repos/{pull_request.repository_slug}/pulls/{pull_request.number}"
        )
        if not isinstance(item, Mapping):
            raise ValueError("invalid pull request response")
        head, base = item.get("head"), item.get("base")
        if (
            item.get("number") != pull_request.number
            or isinstance(item.get("number"), bool)
            or not isinstance(head, Mapping)
            or not isinstance(base, Mapping)
            or head.get("ref") != pull_request.head_branch
            or base.get("ref") != pull_request.base_branch
            or _repo(head.get("repo")) != pull_request.repository_slug
            or _repo(base.get("repo")) != pull_request.repository_slug
        ):
            raise ValueError("pull request identity mismatch")
        state = item.get("state")
        merged_at = item.get("merged_at")
        if state == "open" and merged_at is None:
            return PullRequestState.OPEN
        if state == "closed" and isinstance(merged_at, str) and merged_at:
            return PullRequestState.MERGED
        if state == "closed" and merged_at is None:
            return PullRequestState.CLOSED
        raise ValueError("ambiguous pull request state")


def _repo(value: object) -> str | None:
    if isinstance(value, Mapping):
        name = value.get("full_name")
        if isinstance(name, str):
            return name
    return None
