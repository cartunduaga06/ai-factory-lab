"""GitHub exact-head and CI evidence for post-rebase revalidation."""

from __future__ import annotations

from collections.abc import Mapping

from factory.domain.errors import PullRequestHeadMismatchError
from factory.domain.models import PullRequest
from factory.domain.ports import PostRebaseEvidenceSource
from factory.domain.revalidation import ExactHeadCiEvidence, ExactHeadCiStatus
from factory.integrations.github.client import GitHubClient, GitHubRequestError


class GitHubPostRebaseEvidenceSource(PostRebaseEvidenceSource):
    """Fail closed unless one open PR and its CI match the expected exact head."""

    def __init__(self, client: GitHubClient) -> None:
        self._client = client

    def current_head(self, pull_request: PullRequest) -> str:
        item = self._pull_request(pull_request)
        head = item["head"]
        assert isinstance(head, Mapping)
        sha = head.get("sha")
        if not isinstance(sha, str) or not sha.strip():
            raise PullRequestHeadMismatchError(pull_request.number or 0)
        return sha

    def exact_ci(
        self,
        pull_request: PullRequest,
        expected_head: str,
        required_checks: tuple[str, ...],
        *,
        required: bool,
    ) -> ExactHeadCiEvidence:
        item = self._pull_request(pull_request)
        head = item["head"]
        assert isinstance(head, Mapping)
        provider_head = head.get("sha")
        if provider_head != expected_head:
            raise PullRequestHeadMismatchError(pull_request.number or 0)
        if not required:
            return ExactHeadCiEvidence(expected_head, ExactHeadCiStatus.PASSED, ())
        checks = set(required_checks) if required_checks else self._required_checks(pull_request)
        if not checks:
            return ExactHeadCiEvidence(expected_head, ExactHeadCiStatus.FAILED, ())
        state = self._check_state(pull_request.repository_slug, expected_head, checks)
        return ExactHeadCiEvidence(expected_head, state, tuple(sorted(checks)))

    def _pull_request(self, pull_request: PullRequest) -> Mapping[str, object]:
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
            or item.get("state") != "open"
            or item.get("merged_at") is not None
            or not isinstance(head, Mapping)
            or not isinstance(base, Mapping)
            or head.get("ref") != pull_request.head_branch
            or base.get("ref") != pull_request.base_branch
            or _repo(head) != pull_request.repository_slug
            or _repo(base) != pull_request.repository_slug
        ):
            raise ValueError("pull request identity mismatch")
        return item

    def _required_checks(self, pull_request: PullRequest) -> set[str]:
        try:
            payload = self._client.get(
                f"/repos/{pull_request.repository_slug}/branches/"
                f"{pull_request.base_branch}/protection/required_status_checks"
            )
        except GitHubRequestError as exc:
            if exc.status == 404:
                return set()
            raise
        if not isinstance(payload, Mapping):
            return set()
        contexts, checks = payload.get("contexts"), payload.get("checks", [])
        if not isinstance(contexts, list) or not isinstance(checks, list):
            return set()
        names = {value for value in contexts if isinstance(value, str) and value.strip()}
        for check in checks:
            if not isinstance(check, Mapping) or not isinstance(check.get("context"), str):
                return set()
            names.add(str(check["context"]))
        return names

    def _check_state(self, repository_slug: str, sha: str, required: set[str]) -> ExactHeadCiStatus:
        root = f"/repos/{repository_slug}"
        statuses = self._client.get(f"{root}/commits/{sha}/status")
        runs = self._client.get(f"{root}/commits/{sha}/check-runs", {"per_page": 100})
        if not isinstance(statuses, Mapping) or not isinstance(runs, Mapping):
            return ExactHeadCiStatus.FAILED
        status_rows, check_rows = statuses.get("statuses"), runs.get("check_runs")
        count = runs.get("total_count")
        if (
            not isinstance(status_rows, list)
            or not isinstance(check_rows, list)
            or not isinstance(count, int)
            or count > 100
        ):
            return ExactHeadCiStatus.FAILED
        states: dict[str, bool | None] = {}
        for row in reversed(status_rows):
            if isinstance(row, Mapping) and isinstance(row.get("context"), str):
                raw = row.get("state")
                states[str(row["context"])] = (
                    True
                    if raw == "success"
                    else False
                    if raw
                    in {
                        "failure",
                        "error",
                    }
                    else None
                )
        for row in check_rows:
            if isinstance(row, Mapping) and isinstance(row.get("name"), str):
                if row.get("status") != "completed":
                    states[str(row["name"])] = None
                else:
                    states[str(row["name"])] = row.get("conclusion") == "success"
        values = [states.get(name) for name in required]
        if all(value is True for value in values):
            return ExactHeadCiStatus.PASSED
        if any(value is False for value in values):
            return ExactHeadCiStatus.FAILED
        return ExactHeadCiStatus.PENDING


def _repo(part: Mapping[str, object]) -> str | None:
    repo = part.get("repo")
    if not isinstance(repo, Mapping):
        return None
    value = repo.get("full_name")
    return value if isinstance(value, str) else None


__all__ = ["GitHubPostRebaseEvidenceSource"]
