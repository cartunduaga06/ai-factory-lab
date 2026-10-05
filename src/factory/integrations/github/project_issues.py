"""Read registered project Issues without trusting Issue prose for routing."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace

from factory.domain.enums import RepositoryRole
from factory.domain.errors import ProviderRequestError
from factory.domain.models import FactoryTask, Repository, TaskSource
from factory.domain.ports import IssueSource
from factory.domain.projects import ProjectRegistry

logger = logging.getLogger(__name__)


class ProjectIssueSource(IssueSource):
    """Assign project identity from the registered Issue repository."""

    def __init__(self, source: IssueSource, registry: ProjectRegistry) -> None:
        self._source = source
        self._registry = registry

    def list_open_tasks(self, repository: Repository) -> Sequence[FactoryTask]:
        del repository
        tasks: list[FactoryTask] = []
        for profile in self._registry.profiles:
            repo = Repository(profile.repository_slug, role=RepositoryRole.TARGET)
            try:
                source_tasks = self._source.list_open_tasks(repo)
            except ProviderRequestError as exc:
                logger.warning(
                    "project intake deferred: project_id=%s error_type=%s",
                    profile.project_id,
                    type(exc).__name__,
                )
                continue
            for task in source_tasks:
                if task.source is None or task.source.repository_slug != profile.repository_slug:
                    if task.source is not None:
                        # Keep rejected routing evidence specific and auditable.
                        self._registry.for_repository(task.source.repository_slug)
                    raise ValueError("issue source returned an issue for the wrong repository")
                tasks.append(self._bind(task))
        return tasks

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        if repository.slug != source.repository_slug:
            raise ValueError("issue project repository mismatch")
        self._registry.for_repository(source.repository_slug)
        task = self._source.get_task(repository, source)
        return self._bind(task)

    def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
        if repository.slug != source.repository_slug:
            raise ValueError("issue project repository mismatch")
        self._registry.for_repository(source.repository_slug)
        return self._source.is_eligible(repository, source)

    def _bind(self, task: FactoryTask) -> FactoryTask:
        if task.source is None:
            raise ValueError("project issue has no source")
        profile = self._registry.for_repository(task.source.repository_slug)
        return replace(
            task, target_repository=profile.repository_slug, project_id=profile.project_id
        )
