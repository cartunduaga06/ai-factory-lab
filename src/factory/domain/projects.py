"""Immutable, operator approved project routing identities."""

from __future__ import annotations

import re
from dataclasses import dataclass

from factory.domain.models import QualityGateSpec


class ProjectRoutingError(ValueError):
    """A project identity or repository does not match the approved registry."""


@dataclass(frozen=True, slots=True)
class ProjectProfile:
    """Operator supplied repository, checkout, checks and context for one project."""

    project_id: str
    repository_slug: str
    source_checkout: str
    base_ref: str
    gates: tuple[QualityGateSpec, ...]
    context_profile: str = "repository"
    deploy_policy: str = "human-only"
    git_host: str = "github.com"
    deploy_required: bool = False
    provider_ci_required: bool = True
    required_ci_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", self.project_id):
            raise ProjectRoutingError("invalid project id")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", self.repository_slug):
            raise ProjectRoutingError("invalid project repository")
        if (
            not self.source_checkout
            or not self.base_ref
            or self.context_profile not in {"repository", "repository+ecc"}
        ):
            raise ProjectRoutingError("incomplete project profile")
        if not self.source_checkout.startswith("/"):
            raise ProjectRoutingError("project checkout must be absolute")
        if self.deploy_policy != "human-only":
            raise ProjectRoutingError("unsupported deploy policy")
        if not isinstance(self.deploy_required, bool):
            raise ProjectRoutingError("invalid deploy requirement")
        if not isinstance(self.provider_ci_required, bool):
            raise ProjectRoutingError("invalid provider CI requirement")
        if any(not isinstance(name, str) or not name.strip() for name in self.required_ci_checks):
            raise ProjectRoutingError("invalid required CI check")
        if len(set(self.required_ci_checks)) != len(self.required_ci_checks):
            raise ProjectRoutingError("duplicate required CI check")
        if not self.gates or not any(gate.required for gate in self.gates):
            raise ProjectRoutingError("project requires at least one quality gate")
        if not re.fullmatch(r"[A-Za-z0-9.-]+", self.git_host):
            raise ProjectRoutingError("invalid project git host")


class ProjectRegistry:
    """Resolve an untrusted project id to exactly one approved profile."""

    def __init__(self, profiles: tuple[ProjectProfile, ...]) -> None:
        ids = [profile.project_id for profile in profiles]
        repos = [profile.repository_slug for profile in profiles]
        if not profiles or len(set(ids)) != len(ids) or len(set(repos)) != len(repos):
            raise ProjectRoutingError("duplicate or empty project registry")
        self._profiles = {profile.project_id: profile for profile in profiles}
        self._repositories = {profile.repository_slug: profile for profile in profiles}

    @property
    def profiles(self) -> tuple[ProjectProfile, ...]:
        return tuple(self._profiles.values())

    def resolve(self, project_id: str, repository_slug: str | None = None) -> ProjectProfile:
        profile = self._profiles.get(project_id)
        if profile is None or (
            repository_slug is not None and repository_slug != profile.repository_slug
        ):
            raise ProjectRoutingError("unregistered or mismatched project")
        return profile

    def for_repository(self, repository_slug: str) -> ProjectProfile:
        profile = self._repositories.get(repository_slug)
        if profile is None:
            raise ProjectRoutingError("unregistered project repository")
        return profile
