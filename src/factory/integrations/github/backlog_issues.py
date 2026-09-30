"""Create and recover GitHub Issues identified by a stable WorkItem marker."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from factory.domain.backlog import MaterializedIssue, WorkItem
from factory.domain.ports import BacklogSink
from factory.integrations.github.write_client import GitHubWriteClient, GitHubWriteError


class IssueMaterializationError(RuntimeError):
    """Sanitized issue lookup or write failure."""


def _marker(item: WorkItem) -> str:
    identity = f"{item.provider}\0{item.external_id}".encode()
    return f"<!-- factory-work-item:{hashlib.sha256(identity).hexdigest()} -->"


class GitHubBacklogIssueSink(BacklogSink):
    """Find an exact marker across open and closed Issues before creating one."""

    def __init__(self, client: GitHubWriteClient) -> None:
        self._client = client

    def find_issue(self, item: WorkItem) -> MaterializedIssue | None:
        marker = _marker(item)
        found: MaterializedIssue | None = None
        for page in range(1, 101):
            payload = self._get(
                f"/repos/{item.target_repository}/issues",
                {"state": "all", "per_page": 100, "page": page},
            )
            if not isinstance(payload, list):
                raise IssueMaterializationError("invalid GitHub issue list")
            for candidate in payload:
                if not isinstance(candidate, Mapping) or "pull_request" in candidate:
                    continue
                body = candidate.get("body")
                if isinstance(body, str) and marker in body.splitlines():
                    issue = _issue(candidate, item.target_repository)
                    if found is not None and found.number != issue.number:
                        raise IssueMaterializationError("multiple Issues for one WorkItem")
                    found = issue
            if len(payload) < 100:
                return found
        raise IssueMaterializationError("GitHub issue lookup exceeded page limit")

    def create_issue(self, item: WorkItem) -> MaterializedIssue:
        body = item.body[:60000] + "\n\n" + _marker(item)
        try:
            payload = self._client.post(
                f"/repos/{item.target_repository}/issues",
                {
                    "title": item.title[:200],
                    "body": body,
                    "labels": ["factory-ready"],
                },
            )
        except GitHubWriteError:
            raise IssueMaterializationError("GitHub issue create failed") from None
        return _issue(payload, item.target_repository)

    def _get(self, path: str, params: Mapping[str, str | int]) -> Any:  # noqa: ANN401
        try:
            return self._client.get(path, params)
        except GitHubWriteError:
            raise IssueMaterializationError("GitHub issue lookup failed") from None


def _issue(payload: Any, repository_slug: str) -> MaterializedIssue:  # noqa: ANN401
    if not isinstance(payload, Mapping):
        raise IssueMaterializationError("invalid GitHub issue identity")
    number = payload.get("number")
    url = payload.get("html_url")
    parsed = urlparse(url) if isinstance(url, str) else None
    if (
        not isinstance(number, int)
        or isinstance(number, bool)
        or number <= 0
        or parsed is None
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.path != f"/{repository_slug}/issues/{number}"
        or parsed.query
        or parsed.fragment
    ):
        raise IssueMaterializationError("invalid GitHub issue identity")
    assert isinstance(url, str)
    return MaterializedIssue(repository_slug, number, url)
