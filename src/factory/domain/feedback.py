"""Exact identities and provider facts for post-review reconciliation."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FeedbackIdentity:
    project_id: str
    repository_slug: str
    issue_number: int
    task_id: str
    run_id: str
    workspace_id: str
    commit_sha: str
    pull_request_number: int
    branch: str
    sprint_id: str | None = None
    work_item_provider: str | None = None
    work_item_id: str | None = None


@dataclass(frozen=True, slots=True)
class DeliveryEvidence:
    """Machine verified completion requirements; uncertainty is false."""

    merged: bool
    integration_verified: bool
    required_ci_passed: bool
    deploy_validated: bool
    blockers: bool = False

    @property
    def complete(self) -> bool:
        return (
            self.merged
            and self.integration_verified
            and self.required_ci_passed
            and self.deploy_validated
            and not self.blockers
        )
