"""GitHub Issues as a read-only :class:`~factory.domain.ports.IssueSource`.

This adapter is the only place that knows GitHub's issue JSON shape. It
translates that shape into :class:`~factory.domain.models.FactoryTask` so the
orchestration layer never sees a GitHub payload.

Read-only by construction: the adapter only ever issues ``GET`` requests. It
cannot label, comment on, edit, close or create anything on GitHub.

Eligibility (Phase 2A): an item is a task when it is

* **open** (the API is queried with ``state=open``),
* **not a pull request** (GitHub's issues endpoint also returns PRs, marked by
  a ``pull_request`` key), and
* **labelled** ``factory-ready``.

All other labels are preserved on the task as metadata.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from typing import Any

from factory.domain.models import FactoryTask, Repository, TaskSource
from factory.domain.ports import IssueSource
from factory.integrations.github.client import (
    GitHubClient,
    GitHubRequestError,
)

logger = logging.getLogger(__name__)

#: The label an issue must carry to be picked up by intake.
ELIGIBILITY_LABEL = "factory-ready"

#: GitHub's provider token used in :class:`TaskSource`.
GITHUB_PROVIDER = "github"

#: GitHub's maximum page size for the issues endpoint.
MAX_PER_PAGE = 100
DEFAULT_PER_PAGE = 100

#: Hard cap on pages walked, so a pathological response cannot loop forever.
MAX_PAGES = 100


class GitHubIssueSource(IssueSource):
    """Lists and fetches ``factory-ready`` open issues from GitHub."""

    def __init__(
        self,
        client: GitHubClient,
        target_repository: str | None = None,
        eligibility_label: str = ELIGIBILITY_LABEL,
        per_page: int = DEFAULT_PER_PAGE,
    ) -> None:
        self._client = client
        self._target_repository = target_repository
        self._eligibility_label = eligibility_label
        self._per_page = max(1, min(per_page, MAX_PER_PAGE))

    def __repr__(self) -> str:
        return (
            f"GitHubIssueSource(target_repository={self._target_repository!r},"
            f" eligibility_label={self._eligibility_label!r})"
        )

    def list_open_tasks(self, repository: Repository) -> Sequence[FactoryTask]:
        """Return every eligible open issue in ``repository``, newest page last.

        Walks pages until a short/empty page is returned. Items that are pull
        requests, lack the eligibility label, or are structurally malformed are
        skipped; a malformed item never aborts the whole intake.
        """
        tasks: list[FactoryTask] = []
        for page in range(1, MAX_PAGES + 1):
            items = self._fetch_page(repository.slug, page)
            if not items:
                break
            for item in items:
                if not self._is_eligible(item):
                    continue
                try:
                    tasks.append(self._map_issue(item, repository.slug))
                except ValueError:
                    # A single malformed item must not sink the page.
                    logger.warning("skipping malformed GitHub issue payload")
            if len(items) < self._per_page:
                break
        return tasks

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        """Fetch a single issue by number.

        Raises:
            GitHubRequestError: if the item is a pull request or malformed.
        """
        payload = self._client.get(f"/repos/{repository.slug}/issues/{source.issue_number}")
        if not isinstance(payload, Mapping):
            raise GitHubRequestError(0, "unexpected issue payload")
        if "pull_request" in payload:
            raise GitHubRequestError(
                0, f"#{source.issue_number} in {repository.slug} is a pull request, not an issue"
            )
        return self._map_issue(payload, repository.slug)

    # -- internals ---------------------------------------------------------

    def _fetch_page(self, slug: str, page: int) -> list[Any]:
        payload = self._client.get(
            f"/repos/{slug}/issues",
            {
                "state": "open",
                "per_page": self._per_page,
                "page": page,
            },
        )
        if not isinstance(payload, list):
            raise GitHubRequestError(0, "unexpected issues payload (expected a list)")
        return payload

    def _is_eligible(self, item: Any) -> bool:  # noqa: ANN401
        if not isinstance(item, Mapping):
            return False
        if "pull_request" in item:
            return False
        if item.get("state") != "open":
            return False
        return self._eligibility_label in self._label_names(item)

    def _map_issue(self, item: Mapping[str, Any], source_slug: str) -> FactoryTask:
        number = item.get("number")
        title = item.get("title")
        if not isinstance(number, int) or number <= 0:
            raise ValueError(f"issue is missing a valid number: {number!r}")
        if not isinstance(title, str) or not title.strip():
            raise ValueError(f"issue #{number} is missing a title")

        body = item.get("body")
        source = TaskSource(
            provider=GITHUB_PROVIDER,
            repository_slug=source_slug,
            issue_number=number,
        )
        return FactoryTask(
            title=title,
            target_repository=self._target_repository or source_slug,
            source=source,
            body=body if isinstance(body, str) else "",
            labels=self._label_names(item),
        )

    @staticmethod
    def _label_names(item: Mapping[str, Any]) -> tuple[str, ...]:
        labels = item.get("labels")
        if not isinstance(labels, list):
            return ()
        names: list[str] = []
        for label in labels:
            name = label.get("name") if isinstance(label, Mapping) else label
            if isinstance(name, str) and name:
                names.append(name)
        return tuple(names)


__all__ = [
    "ELIGIBILITY_LABEL",
    "GITHUB_PROVIDER",
    "GitHubIssueSource",
]
