"""Provider verified merge, CI and optional deployment evidence."""

from __future__ import annotations

from collections.abc import Mapping

from factory.domain.feedback import DeliveryEvidence, FeedbackIdentity
from factory.domain.models import PullRequest
from factory.domain.ports import DeliveryEvidenceSource
from factory.domain.projects import ProjectRegistry, ProjectRoutingError
from factory.integrations.github.client import GitHubClient, GitHubError, GitHubRequestError


class GitHubDeliveryEvidenceSource(DeliveryEvidenceSource):
    """Fail closed on ambiguous provider facts or unavailable required checks."""

    def __init__(self, client: GitHubClient, registry: ProjectRegistry) -> None:
        self._client = client
        self._registry = registry

    def current_revision(self, pull_request: PullRequest) -> str:
        if pull_request.number is None or pull_request.number <= 0:
            raise ValueError("persisted pull request has no number")
        item = self._client.get(
            f"/repos/{pull_request.repository_slug}/pulls/{pull_request.number}"
        )
        if not isinstance(item, Mapping) or item.get("number") != pull_request.number:
            raise ValueError("invalid pull request response")
        head, base = item.get("head"), item.get("base")
        if (
            not isinstance(head, Mapping)
            or not isinstance(base, Mapping)
            or head.get("ref") != pull_request.head_branch
            or base.get("ref") != pull_request.base_branch
            or _repository(head) != pull_request.repository_slug
            or _repository(base) != pull_request.repository_slug
            or not isinstance(head.get("sha"), str)
            or not head.get("sha")
        ):
            raise ValueError("pull request identity mismatch")
        return str(head["sha"])

    def evidence(self, identity: FeedbackIdentity) -> DeliveryEvidence:
        merged = False
        try:
            profile = self._registry.resolve(identity.project_id, identity.repository_slug)
            root = f"/repos/{identity.repository_slug}"
            pr = self._client.get(f"{root}/pulls/{identity.pull_request_number}")
            if not isinstance(pr, Mapping) or pr.get("number") != identity.pull_request_number:
                return _unverified()
            head, base = pr.get("head"), pr.get("base")
            if (
                not isinstance(head, Mapping)
                or not isinstance(base, Mapping)
                or head.get("sha") != identity.commit_sha
                or head.get("ref") != identity.branch
                or _repository(head) != identity.repository_slug
                or _repository(base) != identity.repository_slug
                or base.get("ref") != profile.base_ref
                or not pr.get("merged")
                or pr.get("state") != "closed"
                or not isinstance(pr.get("merged_at"), str)
                or not isinstance(pr.get("merge_commit_sha"), str)
            ):
                return _unverified()
            merged = True
            merged_sha = str(pr["merge_commit_sha"])
            commit = self._client.get(f"{root}/commits/{merged_sha}")
            integrated = isinstance(commit, Mapping) and commit.get("sha") == merged_sha
            if profile.provider_ci_required:
                required = self._required_checks(root, profile.base_ref, profile.required_ci_checks)
                ci = self._checks_pass(root, identity.commit_sha, required)
            else:
                # A persisted SUCCEEDED run has already passed the project's required
                # local gates before publication. This explicit operator-owned policy
                # avoids requiring GitHub branch-protection/status/check permissions.
                ci = True
            deployed = not profile.deploy_required or self._deployment_passed(root, merged_sha)
            return DeliveryEvidence(True, integrated, ci, deployed)
        except (GitHubError, ProjectRoutingError, KeyError, TypeError, ValueError):
            return DeliveryEvidence(merged, False, False, False)

    def _required_checks(
        self, root: str, branch: str, configured: tuple[str, ...]
    ) -> set[str] | None:
        if configured:
            return set(configured)
        try:
            payload = self._client.get(
                f"{root}/branches/{branch}/protection/required_status_checks"
            )
        except GitHubRequestError as exc:
            if exc.status == 404:
                return set()
            raise
        if not isinstance(payload, Mapping):
            return None
        contexts = payload.get("contexts")
        checks = payload.get("checks", [])
        if not isinstance(contexts, list) or not isinstance(checks, list):
            return None
        names = {value for value in contexts if isinstance(value, str)}
        for check in checks:
            if not isinstance(check, Mapping) or not isinstance(check.get("context"), str):
                return None
            names.add(str(check["context"]))
        return names

    def _checks_pass(self, root: str, sha: str, required: set[str] | None) -> bool:
        if required is None:
            return False
        statuses = self._client.get(f"{root}/commits/{sha}/status")
        runs = self._client.get(f"{root}/commits/{sha}/check-runs", {"per_page": 100})
        if not isinstance(statuses, Mapping) or not isinstance(runs, Mapping):
            return False
        status_rows = statuses.get("statuses")
        check_rows = runs.get("check_runs")
        count = runs.get("total_count")
        if (
            not isinstance(status_rows, list)
            or not isinstance(check_rows, list)
            or not isinstance(count, int)
            or count > 100
        ):
            return False
        state: dict[str, bool] = {}
        for row in reversed(status_rows):
            if (
                isinstance(row, Mapping)
                and row.get("sha") == sha
                and isinstance(row.get("context"), str)
            ):
                state[str(row["context"])] = row.get("state") == "success"
        for row in check_rows:
            # The SHA-scoped endpoint is useful routing, but the returned run
            # itself is the evidence. Require its immutable identity too so a
            # stale or malformed provider row cannot satisfy this delivery.
            if (
                isinstance(row, Mapping)
                and row.get("head_sha") == sha
                and isinstance(row.get("id"), int)
                and not isinstance(row.get("id"), bool)
                and row.get("id", 0) > 0
                and isinstance(row.get("name"), str)
            ):
                state[str(row["name"])] = (
                    row.get("status") == "completed" and row.get("conclusion") == "success"
                )
        return all(state.get(name) is True for name in required)

    def _deployment_passed(self, root: str, sha: str) -> bool:
        deployments = self._client.get(f"{root}/deployments", {"sha": sha, "per_page": 100})
        if not isinstance(deployments, list) or len(deployments) >= 100:
            return False
        for deployment in deployments:
            if not isinstance(deployment, Mapping) or not isinstance(deployment.get("id"), int):
                continue
            statuses = self._client.get(
                f"{root}/deployments/{deployment['id']}/statuses", {"per_page": 100}
            )
            if (
                isinstance(statuses, list)
                and statuses
                and isinstance(statuses[0], Mapping)
                and statuses[0].get("state") == "success"
            ):
                return True
        return False


def _repository(part: Mapping[str, object]) -> str | None:
    repo = part.get("repo")
    return str(repo.get("full_name")) if isinstance(repo, Mapping) else None


def _unverified() -> DeliveryEvidence:
    return DeliveryEvidence(False, False, False, False)
