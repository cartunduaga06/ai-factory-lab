"""Provider-neutral backlog identities and materialization facts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkItem:
    """A snapshot from a backlog; eligibility must be checked again before writing."""

    provider: str
    external_id: str
    title: str
    body: str
    target_repository: str
    eligible: bool
    dependencies_satisfied: bool
    project_id: str = "ai-factory-lab"

    def __post_init__(self) -> None:
        if not self.provider or not self.external_id or not self.title.strip():
            raise ValueError("work item identity and title are required")
        if self.target_repository.count("/") != 1:
            raise ValueError("work item target repository must be owner/name")
        if not self.project_id:
            raise ValueError("work item project id is required")


@dataclass(frozen=True, slots=True)
class MaterializedIssue:
    """The provider-confirmed issue identity, without provider prose."""

    repository_slug: str
    number: int
    url: str
