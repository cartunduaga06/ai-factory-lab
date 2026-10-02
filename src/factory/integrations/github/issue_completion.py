"""Idempotent, identity checked GitHub Issue completion."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.parse import urlparse

from factory.domain.feedback import FeedbackIdentity
from factory.domain.ports import IssueCompletionSink
from factory.integrations.github.write_client import GitHubWriteClient, GitHubWriteError


class IssueCompletionError(RuntimeError):
    """An Issue cannot safely be completed; no provider text escapes."""


class GitHubIssueCompletionSink(IssueCompletionSink):
    def __init__(self, client: GitHubWriteClient) -> None:
        self._client = client

    def complete(self, identity: FeedbackIdentity) -> None:
        path = f"/repos/{identity.repository_slug}/issues/{identity.issue_number}"
        marker = f"<!-- factory-delivery:{identity.task_id} -->"
        try:
            issue = self._client.get(path)
            self._check_issue(issue, identity)
            if not self._has_comment(path, marker):
                self._client.post(
                    path + "/comments",
                    {
                        "body": "Factory delivery verified. "
                        f"Task: {identity.task_id}; PR: #{identity.pull_request_number}; "
                        f"commit: {identity.commit_sha}.\n{marker}"
                    },
                )
            labels = issue.get("labels")
            assert isinstance(labels, list)
            remaining = [
                str(label["name"])
                for label in labels
                if isinstance(label, Mapping) and label.get("name") != "factory-ready"
            ]
            if len(remaining) != len(labels):
                self._client.patch(path, {"labels": remaining})
            if issue.get("state") != "closed" or issue.get("state_reason") != "completed":
                closed = self._client.patch(path, {"state": "closed", "state_reason": "completed"})
                self._check_issue(closed, identity)
                if closed.get("state") != "closed" or closed.get("state_reason") != "completed":
                    raise IssueCompletionError("Issue closure was not confirmed")
            final = self._client.get(path)
            self._check_issue(final, identity)
            assert isinstance(final, Mapping)
            if (
                final.get("state") != "closed"
                or final.get("state_reason") != "completed"
                or any(
                    isinstance(label, Mapping) and label.get("name") == "factory-ready"
                    for label in final["labels"]
                )
            ):
                raise IssueCompletionError("Issue completion was not confirmed")
        except GitHubWriteError:
            raise IssueCompletionError("GitHub Issue completion failed") from None

    def state(self, repository_slug: str, issue_number: int) -> tuple[str, str | None]:
        path = f"/repos/{repository_slug}/issues/{issue_number}"
        try:
            issue = self._client.get(path)
        except Exception:
            raise IssueCompletionError("GitHub Issue read failed") from None
        if (
            not isinstance(issue, Mapping)
            or isinstance(issue.get("number"), bool)
            or issue.get("number") != issue_number
            or "pull_request" in issue
            or issue.get("state") not in {"open", "closed"}
            or (
                issue.get("state") == "closed"
                and issue.get("state_reason") not in {"completed", "not_planned", "duplicate"}
            )
        ):
            raise IssueCompletionError("Issue state is ambiguous")
        value = issue.get("state_reason")
        return str(issue["state"]), str(value) if value is not None else None

    def close(self, identity: FeedbackIdentity, reason: str) -> None:
        if reason not in {"completed", "not_planned", "duplicate"}:
            raise IssueCompletionError("unsupported Issue closure reason")
        path = f"/repos/{identity.repository_slug}/issues/{identity.issue_number}"
        try:
            issue = self._client.get(path)
            self._check_issue(issue, identity)
            if issue.get("state") == "closed" and issue.get("state_reason") == reason:
                return
            marker = f"<!-- factory-resolution:{identity.task_id}:{reason} -->"
            if not self._has_comment(path, marker):
                self._client.post(
                    path + "/comments", {"body": f"Factory resolution: {reason}. {marker}"}
                )
            if issue.get("state") != "closed" or issue.get("state_reason") != reason:
                closed = self._client.patch(path, {"state": "closed", "state_reason": reason})
                self._check_issue(closed, identity)
                if closed.get("state") != "closed" or closed.get("state_reason") != reason:
                    raise IssueCompletionError("Issue closure was not confirmed")
            final = self._client.get(path)
            self._check_issue(final, identity)
            if final.get("state") != "closed" or final.get("state_reason") != reason:
                raise IssueCompletionError("Issue closure was not confirmed")
        except GitHubWriteError:
            raise IssueCompletionError("GitHub Issue closure failed") from None

    def _has_comment(self, path: str, marker: str) -> bool:
        for page in range(1, 101):
            payload = self._client.get(path + "/comments", {"per_page": 100, "page": page})
            if not isinstance(payload, list):
                raise IssueCompletionError("invalid Issue comments")
            if any(
                isinstance(comment, Mapping)
                and isinstance(comment.get("body"), str)
                and marker in comment["body"].splitlines()
                for comment in payload
            ):
                return True
            if len(payload) < 100:
                return False
        raise IssueCompletionError("Issue comment lookup exceeded page limit")

    @staticmethod
    def _check_issue(issue: object, identity: FeedbackIdentity) -> None:
        if not isinstance(issue, Mapping):
            raise IssueCompletionError("invalid Issue identity")
        url = issue.get("html_url")
        parsed = urlparse(url) if isinstance(url, str) else None
        labels = issue.get("labels")
        if (
            issue.get("number") != identity.issue_number
            or "pull_request" in issue
            or parsed is None
            or parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.path != f"/{identity.repository_slug}/issues/{identity.issue_number}"
            or parsed.query
            or parsed.fragment
            or issue.get("state") not in {"open", "closed"}
            or not isinstance(labels, list)
            or not all(
                isinstance(label, Mapping) and isinstance(label.get("name"), str)
                for label in labels
            )
        ):
            raise IssueCompletionError("Issue identity mismatch")
