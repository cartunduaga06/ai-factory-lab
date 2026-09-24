"""Real (non-mock) publication doubles for orchestration tests.

These are self-contained implementations of the Phase 5 ports, not mocks of the
code under test. They let ``PublicationService`` be exercised deterministically
without git or the network, while the concrete Git publisher and GitHub sink have
their own dedicated tests against a temporary repository and an injected
transport.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from factory.domain.enums import RepositoryRole
from factory.domain.errors import PublicationError
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PublishedRevision,
    PullRequest,
    Repository,
)
from factory.domain.ports import PullRequestSink, WorkspacePublisher


class FakeWorkspacePublisher(WorkspacePublisher):
    """Returns a deterministic revision and records each publication.

    ``commit_sha`` is stable for a given branch, so a retry reuses the same
    revision — mirroring the concrete publisher's idempotency. ``fail_with`` drives
    the failure path; the exception is normalized like the real publisher.
    """

    def __init__(self, *, fail_with: Exception | None = None) -> None:
        self._fail_with = fail_with
        self.published: list[tuple[str, str]] = []
        #: Number of times publish() was actually invoked.
        self.calls = 0

    def publish(self, task: FactoryTask, run: AgentRun) -> PublishedRevision:
        self.calls += 1
        if self._fail_with is not None:
            raise PublicationError(f"workspace {run.workspace and run.workspace.workspace_id}")
        workspace = run.workspace
        assert workspace is not None
        self.published.append((task.task_id, workspace.branch))
        return PublishedRevision(commit_sha=f"sha-{workspace.branch}", branch=workspace.branch)


class FakePullRequestSink(PullRequestSink):
    """An in-memory provider: remembers open PRs and opens each at most once."""

    def __init__(self, *, fail_create: bool = False) -> None:
        self._fail_create = fail_create
        #: Open PRs keyed by (repository_slug, head_branch).
        self._open: dict[tuple[str, str], PullRequest] = {}
        self.find_calls = 0
        self.create_calls = 0
        self.next_number = 100

    def find_open_pull_request(
        self, repository: Repository, head_branch: str
    ) -> PullRequest | None:
        self.find_calls += 1
        return self._open.get((repository.slug, head_branch))

    def open_pull_request(self, pull_request: PullRequest) -> PullRequest:
        self.create_calls += 1
        if self._fail_create:
            raise PublicationError("provider refused to open a pull request")
        key = (pull_request.repository_slug, pull_request.head_branch)
        existing = self._open.get(key)
        if existing is not None:
            return existing
        self.next_number += 1
        opened = PullRequest(
            repository_slug=pull_request.repository_slug,
            head_branch=pull_request.head_branch,
            base_branch=pull_request.base_branch,
            title=pull_request.title,
            body=pull_request.body,
            number=self.next_number,
            url=f"https://example.invalid/{self.next_number}",
            task_id=pull_request.task_id,
            run_id=pull_request.run_id,
        )
        self._open[key] = opened
        return opened

    def seed(self, pull_request: PullRequest) -> None:
        """Pre-populate the provider with an already-open PR (crash window C)."""
        self._open[(pull_request.repository_slug, pull_request.head_branch)] = pull_request


class FakeWriteTransport:
    """An injectable GitHub write transport for sink tests; never hits the network."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self._responses = list(responses or [])
        self.requests: list[tuple[str, str, Mapping[str, str], Mapping[str, Any] | None]] = []

    def request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> Any:  # noqa: ANN401
        self.requests.append((method, url, headers, body))
        if not self._responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def target_repository(slug: str = "example/target") -> Repository:
    return Repository(slug=slug, role=RepositoryRole.TARGET)


__all__ = [
    "FakePullRequestSink",
    "FakeWorkspacePublisher",
    "FakeWriteTransport",
    "target_repository",
]
