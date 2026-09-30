"""Concrete checkout and context routing from approved project profiles."""

from __future__ import annotations

import subprocess
from urllib.parse import urlsplit

from factory.domain.context import ContextFragment
from factory.domain.models import FactoryTask, Workspace
from factory.domain.ports import WorkspaceProvisioner
from factory.domain.projects import ProjectProfile, ProjectRegistry, ProjectRoutingError
from factory.integrations.context.repository import RepositoryContextSource
from factory.integrations.context.skill_source import ApprovedSkillSource
from factory.integrations.workspace.git import GitWorktreeWorkspaceProvisioner


def _verify_checkout(profile: ProjectProfile) -> None:
    """Require the configured checkout's origin to name its approved repository."""
    try:
        result = subprocess.run(
            ["git", "-C", profile.source_checkout, "config", "--get", "remote.origin.url"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise ProjectRoutingError("project checkout identity unavailable") from None
    if result.returncode != 0:
        raise ProjectRoutingError("project checkout identity unavailable")
    url = urlsplit(result.stdout.decode("utf-8", errors="replace").strip())
    try:
        port = url.port
    except ValueError:
        raise ProjectRoutingError("project checkout identity mismatch") from None
    if (
        url.scheme != "https"
        or url.hostname != profile.git_host
        or url.username is not None
        or url.password is not None
        or port is not None
        or url.path not in (f"/{profile.repository_slug}", f"/{profile.repository_slug}.git")
        or url.query
        or url.fragment
    ):
        raise ProjectRoutingError("project checkout identity mismatch")


class ProjectWorkspaceProvisioner(WorkspaceProvisioner):
    """Select a registered source checkout and base ref for each run."""

    def __init__(self, registry: ProjectRegistry) -> None:
        self._registry = registry

    def _for(self, task: FactoryTask, workspace: Workspace) -> GitWorktreeWorkspaceProvisioner:
        profile = self._registry.resolve(task.project_id, task.target_repository)
        if workspace.repository_slug != profile.repository_slug:
            raise ValueError("workspace project identity mismatch")
        _verify_checkout(profile)
        return GitWorktreeWorkspaceProvisioner(profile.source_checkout, base_ref=profile.base_ref)

    def prepare(self, task: FactoryTask, workspace: Workspace) -> Workspace:
        return self._for(task, workspace).prepare(task, workspace)

    def repair(self, task: FactoryTask, workspace: Workspace) -> Workspace:
        return self._for(task, workspace).repair(task, workspace)


class ProjectContextSource:
    """Read E5 repository context only from the registered project's checkout."""

    required = True

    def __init__(self, registry: ProjectRegistry) -> None:
        self._registry = registry

    def fragments(self, task: FactoryTask) -> tuple[ContextFragment, ...]:
        profile = self._registry.resolve(task.project_id, task.target_repository)
        _verify_checkout(profile)
        return RepositoryContextSource(profile.source_checkout).fragments(task)


class ProjectSkillSource:
    """Include pinned ECC guidance only for profiles that approve it."""

    required = False

    def __init__(self, registry: ProjectRegistry, selection: str | None) -> None:
        self._registry = registry
        self._selection = selection

    def fragments(self, task: FactoryTask) -> tuple[ContextFragment, ...]:
        profile = self._registry.resolve(task.project_id, task.target_repository)
        if profile.context_profile == "repository":
            return ()
        if self._selection is None:
            raise ProjectRoutingError("approved project context unavailable")
        return ApprovedSkillSource(self._selection).fragments(task)
