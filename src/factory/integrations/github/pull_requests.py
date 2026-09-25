"""GitHub as a :class:`~factory.domain.ports.PullRequestSink`.

This is the only Phase 5 component that talks to GitHub about pull requests. It
uses the write-capable :class:`~factory.integrations.github.write_client.GitHubWriteClient`,
whose transport is injectable, so tests never reach the network.

The sink can **find** an open PR by head branch and **open** one. It deliberately
exposes no merge, close or issue-mutation operation: the factory may open a PR and
then stops at ``WAITING_HUMAN``.

Remote response bodies are untrusted provider text. This adapter reads only the
trusted numeric ``number`` and the PR ``url`` to enrich a
:class:`~factory.domain.models.PullRequest`; it never copies a GitHub ``message``,
``error`` or ``detail`` into an exception. Numeric HTTP status is the only error
detail that escapes, via
:class:`~factory.integrations.github.write_client.GitHubWriteError`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from factory.domain.errors import PublicationError
from factory.domain.models import PullRequest, Repository
from factory.domain.ports import PullRequestSink
from factory.integrations.github.write_client import GitHubWriteClient, GitHubWriteError

#: GitHub caps PR titles at 256 characters. Bound well below it, and strip any
#: non-printable characters, so provider/agent text cannot smuggle anything odd in.
MAX_TITLE_LENGTH = 200

DEFAULT_BASE_BRANCH = "main"


class GitHubPullRequestSink(PullRequestSink):
    """Finds and opens pull requests on GitHub."""

    def __init__(self, client: GitHubWriteClient) -> None:
        self._client = client

    def __repr__(self) -> str:
        return f"GitHubPullRequestSink(client={self._client!r})"

    def find_open_pull_request(
        self, repository: Repository, head_branch: str, base_branch: str
    ) -> PullRequest | None:
        """Return the open PR from ``head_branch`` into ``base_branch``, if any.

        Exact provider identity is required: ``state == open``, ``head.ref`` ==
        ``head_branch``, ``head.repo.full_name`` == the target repository,
        ``base.ref`` == ``base_branch``, and (when GitHub supplies it)
        ``base.repo.full_name`` == the target repository. A PR with the right head
        branch but a different base, or a head from a fork, is not a match — so a
        same-named branch elsewhere can never be mistaken for the factory's
        publication. If an earlier item does not match, the search continues.
        """
        if not base_branch:
            return None
        return self._find_open(repository.slug, head_branch, base_branch)

    def open_pull_request(self, pull_request: PullRequest) -> PullRequest:
        """Open a PR, or return the existing open one for the same head branch.

        Retry-safe: if GitHub rejects the create because an open PR already exists
        for this head branch, that PR is looked up and returned instead of failing
        — but only when it matches the *full* expected identity (repository, head
        repository, head branch, base branch). The create failure is reduced to its
        numeric status before any fallback, so no provider text is inspected or
        propagated.
        """
        base_branch = self._expected_base(pull_request)
        payload, status = self._attempt(
            lambda: self._client.post(
                f"/repos/{pull_request.repository_slug}/pulls",
                {
                    "title": _bound_title(pull_request.title),
                    "head": pull_request.head_branch,
                    "base": base_branch,
                    "body": pull_request.body,
                },
            )
        )
        if status is not None:
            existing = self._find_open(
                pull_request.repository_slug, pull_request.head_branch, base_branch
            )
            if existing is not None:
                return _with_metadata(existing, pull_request)
            # Raised outside the ``except`` block so the client's exception is not
            # retained as ``__context__``: only the sanitized status escapes.
            raise PublicationError(f"GitHub pull request create failed with status {status}")

        mapped = _map_created(payload, pull_request)
        if mapped is not None:
            return mapped

        existing = self._find_open(
            pull_request.repository_slug, pull_request.head_branch, base_branch
        )
        if existing is not None:
            return _with_metadata(existing, pull_request)
        raise PublicationError(f"pull request for run {pull_request.run_id} could not be opened")

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _expected_base(pull_request: PullRequest) -> str:
        return pull_request.base_branch or DEFAULT_BASE_BRANCH

    def _find_open(
        self, repository_slug: str, head_branch: str, base_branch: str
    ) -> PullRequest | None:
        owner = repository_slug.split("/", 1)[0]
        payload = self._get(
            f"/repos/{repository_slug}/pulls",
            {"state": "open", "head": f"{owner}:{head_branch}"},
        )
        if not isinstance(payload, Sequence) or isinstance(payload, str):
            return None
        for item in payload:
            mapped = _map_pull_request(item, repository_slug, head_branch, base_branch)
            if mapped is not None:
                return mapped
        return None

    def _get(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:  # noqa: ANN401
        value, status = self._attempt(lambda: self._client.get(path, params))
        if status is not None:
            # Raised outside the ``except`` block so the client's exception is not
            # retained as ``__context__``: only the sanitized status escapes.
            raise PublicationError(f"GitHub pull request lookup failed with status {status}")
        return value

    @staticmethod
    def _attempt(action: Callable[[], Any]) -> tuple[Any, int | None]:  # noqa: ANN401
        """Run ``action``; return ``(value, None)`` or ``(None, status)`` on failure."""
        try:
            return action(), None
        except GitHubWriteError as exc:
            return None, exc.status


def _map_created(payload: Any, pull_request: PullRequest) -> PullRequest | None:  # noqa: ANN401
    number = payload.get("number") if isinstance(payload, Mapping) else None
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None
    url = payload.get("html_url")
    return PullRequest(
        repository_slug=pull_request.repository_slug,
        head_branch=pull_request.head_branch,
        base_branch=pull_request.base_branch or DEFAULT_BASE_BRANCH,
        title=pull_request.title,
        body=pull_request.body,
        number=number,
        url=url if isinstance(url, str) else None,
        task_id=pull_request.task_id,
        run_id=pull_request.run_id,
        opened_at=pull_request.opened_at,
    )


def _map_pull_request(
    item: Any,  # noqa: ANN401
    repository_slug: str,
    head_branch: str,
    base_branch: str,
) -> PullRequest | None:
    """Map a provider PR only when its full identity matches the expectation.

    Untrusted: every field is read structurally and a missing, malformed or
    different value makes the item *not* a match (so the caller keeps searching),
    never an error. Provider payload content is never copied into a result or an
    exception.
    """
    if not isinstance(item, Mapping):
        return None
    if item.get("state") != "open":
        return None
    number = item.get("number")
    if not isinstance(number, int) or isinstance(number, bool) or number <= 0:
        return None

    head = item.get("head")
    if not isinstance(head, Mapping):
        return None
    if head.get("ref") != head_branch:
        return None
    if _repo_full_name(head.get("repo")) != repository_slug:
        return None

    base = item.get("base")
    if not isinstance(base, Mapping):
        return None
    if base.get("ref") != base_branch:
        return None
    base_repo = base.get("repo")
    # ``base.repo`` is not always present; when GitHub does supply it, it must be
    # the target repository too.
    if base_repo is not None and _repo_full_name(base_repo) != repository_slug:
        return None

    title = item.get("title")
    url = item.get("html_url")
    return PullRequest(
        repository_slug=repository_slug,
        head_branch=head_branch,
        base_branch=base_branch,
        title=_bound_title(title if isinstance(title, str) else "factory pull request"),
        number=number,
        url=url if isinstance(url, str) else None,
    )


def _repo_full_name(repo: Any) -> str | None:  # noqa: ANN401
    """Return ``repo.full_name`` when it is a well-formed ``owner/name`` string."""
    if not isinstance(repo, Mapping):
        return None
    full_name = repo.get("full_name")
    if not isinstance(full_name, str) or full_name.count("/") != 1:
        return None
    owner, name = full_name.split("/", 1)
    if not owner or not name:
        return None
    return full_name


def _with_metadata(found: PullRequest, source: PullRequest) -> PullRequest:
    """Re-attach the factory's own run/task identity and body to a recovered PR."""
    return PullRequest(
        repository_slug=found.repository_slug,
        head_branch=found.head_branch,
        base_branch=found.base_branch,
        title=found.title,
        body=source.body,
        number=found.number,
        url=found.url,
        task_id=source.task_id,
        run_id=source.run_id,
        opened_at=source.opened_at,
    )


def _bound_title(title: str) -> str:
    """Return a bounded, non-printable-free title for a PR."""
    cleaned = "".join(ch for ch in title if ch.isprintable()).strip()
    if not cleaned:
        return "factory pull request"
    return cleaned[:MAX_TITLE_LENGTH]


__all__ = [
    "DEFAULT_BASE_BRANCH",
    "MAX_TITLE_LENGTH",
    "GitHubPullRequestSink",
]
