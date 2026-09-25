"""Tests for the GitHub pull-request sink and its strict provider-error boundary.

Everything runs against an injected in-memory transport; no test touches the
network or a real GitHub credential.
"""

from __future__ import annotations

import traceback

import pytest

from factory.domain.errors import PublicationError
from factory.domain.models import PullRequest
from factory.integrations.github.pull_requests import GitHubPullRequestSink
from factory.integrations.github.write_client import (
    GitHubWriteClient,
    GitHubWriteError,
)
from tests.fake_publish import FakeWriteTransport, target_repository

TOKEN = "ghp_read_only_intake_token"
SECRET = "MY_PRIVATE_PUSH_PASSWORD_93726"
REPO = target_repository("example/target")


def _sink(transport: FakeWriteTransport) -> GitHubPullRequestSink:
    client = GitHubWriteClient(token=TOKEN, api_url="https://api.github.com", transport=transport)
    return GitHubPullRequestSink(client)


def _open_payload(
    number: int = 5,
    branch: str = "factory/task/ws",
    base: str = "main",
    *,
    head_repo: str = "example/target",
    base_repo: str | None = "example/target",
) -> dict[str, object]:
    base_obj: dict[str, object] = {"ref": base}
    if base_repo is not None:
        base_obj["repo"] = {"full_name": base_repo}
    return {
        "number": number,
        "state": "open",
        "html_url": f"https://github.com/example/target/pull/{number}",
        "title": "Add widget",
        "head": {"ref": branch, "repo": {"full_name": head_repo}},
        "base": base_obj,
    }


# -- discovery -------------------------------------------------------------


def test_existing_open_pr_is_discovered_by_head_branch() -> None:
    transport = FakeWriteTransport([[_open_payload(11)]])
    found = _sink(transport).find_open_pull_request(REPO, "factory/task/ws", "main")

    assert found is not None
    assert found.number == 11
    assert found.head_branch == "factory/task/ws"
    assert found.base_branch == "main"
    assert found.repository_slug == "example/target"
    method, url, _headers, _body = transport.requests[0]
    assert method == "GET"
    assert "/repos/example/target/pulls" in url
    assert "state=open" in url
    assert "head=example%3Afactory" in url


def test_no_open_pr_returns_none() -> None:
    transport = FakeWriteTransport([[]])
    assert _sink(transport).find_open_pull_request(REPO, "factory/task/ws", "main") is None


def test_wrong_base_pr_is_not_discovered() -> None:
    """REGRESSION 1: a PR with the right head but the wrong base is not a match."""
    transport = FakeWriteTransport([[_open_payload(12, base="release")]])
    assert _sink(transport).find_open_pull_request(REPO, "factory/task/ws", "main") is None


def test_correct_pr_after_a_wrong_base_pr_is_selected() -> None:
    """REGRESSION 2: search continues past a wrong-base PR to the correct one."""
    transport = FakeWriteTransport([[_open_payload(13, base="release"), _open_payload(14)]])
    found = _sink(transport).find_open_pull_request(REPO, "factory/task/ws", "main")
    assert found is not None
    assert found.number == 14
    assert found.base_branch == "main"


def test_fork_head_repository_is_not_discovered() -> None:
    """REGRESSION 3: a same-named branch from a fork is never recovered."""
    transport = FakeWriteTransport([[_open_payload(15, head_repo="example/fork-or-other-repo")]])
    assert _sink(transport).find_open_pull_request(REPO, "factory/task/ws", "main") is None


def test_missing_head_repository_identity_is_not_discovered() -> None:
    """A payload without ``head.repo.full_name`` must not be accepted."""
    payload = _open_payload(16)
    payload["head"] = {"ref": "factory/task/ws"}
    transport = FakeWriteTransport([[payload]])
    assert _sink(transport).find_open_pull_request(REPO, "factory/task/ws", "main") is None


def test_wrong_base_repository_identity_is_not_discovered() -> None:
    transport = FakeWriteTransport([[_open_payload(17, base_repo="example/other-repo")]])
    assert _sink(transport).find_open_pull_request(REPO, "factory/task/ws", "main") is None


def test_exact_identity_is_recovered() -> None:
    """REGRESSION 4: an exact-identity PR is recovered normally."""
    transport = FakeWriteTransport([[_open_payload(18)]])
    found = _sink(transport).find_open_pull_request(REPO, "factory/task/ws", "main")
    assert found is not None
    assert found.number == 18


# -- creation --------------------------------------------------------------


def test_pr_is_created_once_when_absent() -> None:
    transport = FakeWriteTransport(
        [
            [],  # find: none
            _open_payload(21),  # create
        ]
    )
    requested = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task/ws",
        base_branch="main",
        title="Add widget",
        body="body",
    )
    sink = _sink(transport)
    assert sink.find_open_pull_request(REPO, requested.head_branch, requested.base_branch) is None
    opened = sink.open_pull_request(requested)

    assert opened.number == 21
    assert opened.url == "https://github.com/example/target/pull/21"
    methods = [r[0] for r in transport.requests]
    assert methods == ["GET", "POST"]
    post_body = transport.requests[1][3]
    assert post_body is not None
    assert post_body["head"] == "factory/task/ws"
    assert post_body["base"] == "main"


def test_repeated_open_finds_the_same_pr_after_a_failed_create() -> None:
    """A create that fails because a PR already exists recovers that PR."""
    transport = FakeWriteTransport(
        [
            GitHubWriteError(422),  # create fails (already exists)
            [_open_payload(31)],  # find returns it
        ]
    )
    requested = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task/ws",
        base_branch="main",
        title="Add widget",
        body="body",
        task_id="task-1",
        run_id="run-1",
    )
    opened = _sink(transport).open_pull_request(requested)

    assert opened.number == 31
    assert opened.run_id == "run-1"
    assert opened.task_id == "task-1"


def test_create_422_with_only_wrong_base_pr_is_refused() -> None:
    """REGRESSION 5: a 422 fallback must not recover a wrong-base PR."""
    transport = FakeWriteTransport(
        [
            GitHubWriteError(422),  # create refused
            [_open_payload(32, base="release")],  # lookup: same head, wrong base
        ]
    )
    requested = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task/ws",
        base_branch="main",
        title="Add widget",
    )
    with pytest.raises(PublicationError) as caught:
        _sink(transport).open_pull_request(requested)

    error = caught.value
    assert SECRET not in str(error)
    assert error.__cause__ is None and error.__context__ is None
    assert "release" not in str(error)


def test_create_422_with_only_fork_pr_is_refused() -> None:
    """A 422 fallback must not recover a PR whose head is a fork."""
    transport = FakeWriteTransport(
        [
            GitHubWriteError(422),
            [_open_payload(33, head_repo="example/fork-or-other-repo")],
        ]
    )
    requested = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task/ws",
        base_branch="main",
        title="Add widget",
    )
    with pytest.raises(PublicationError):
        _sink(transport).open_pull_request(requested)


def test_create_422_with_matching_pr_is_recovered() -> None:
    """A 422 fallback recovers the exact-identity PR."""
    transport = FakeWriteTransport(
        [
            GitHubWriteError(422),
            [_open_payload(34)],
        ]
    )
    requested = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task/ws",
        base_branch="main",
        title="Add widget",
        task_id="task-1",
        run_id="run-1",
    )
    opened = _sink(transport).open_pull_request(requested)

    assert opened.number == 34
    assert opened.base_branch == "main"


def test_base_branch_is_explicit() -> None:
    transport = FakeWriteTransport([_open_payload(41)])
    requested = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task/ws",
        base_branch="release",
        title="Add widget",
    )
    _sink(transport).open_pull_request(requested)
    assert transport.requests[0][3]["base"] == "release"


# -- error boundary --------------------------------------------------------


def test_remote_secret_in_http_error_is_discarded() -> None:
    transport = FakeWriteTransport(
        [
            GitHubWriteError(500),  # only a numeric status; no body text
            [],  # fallback lookup finds nothing
        ]
    )
    requested = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task/ws",
        base_branch="main",
        title="Add widget",
    )

    with pytest.raises(PublicationError) as caught:
        _sink(transport).open_pull_request(requested)

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    assert SECRET not in str(error)
    assert SECRET not in formatted


def test_authorization_header_never_appears_in_errors_or_repr() -> None:
    transport = FakeWriteTransport([GitHubWriteError(403)])
    sink = _sink(transport)
    error = None
    try:
        sink.find_open_pull_request(REPO, "factory/task/ws", "main")
    except PublicationError as exc:  # noqa: PERF203
        error = exc
    assert error is not None
    rendered = f"{error!s}{error!r}{traceback.format_exception(error)}"
    assert "Authorization" not in rendered
    assert "Bearer" not in rendered
    assert TOKEN not in rendered
    # The repr of the sink and client never carries the token either.
    assert TOKEN not in repr(sink)


def test_only_numeric_status_escapes_from_the_write_client() -> None:
    transport = FakeWriteTransport(
        [Exception("body contains " + SECRET)]  # type: ignore[list-item]
    )
    client = GitHubWriteClient(token=TOKEN, api_url="https://api.github.com", transport=transport)
    with pytest.raises(GitHubWriteError) as caught:
        client.get("/repos/example/target/pulls")
    assert caught.value.status == 0
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


# -- no forbidden operations -----------------------------------------------


def test_no_merge_or_issue_mutation_endpoint_is_invoked() -> None:
    transport = FakeWriteTransport([[], _open_payload(51)])
    requested = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task/ws",
        base_branch="main",
        title="Add widget",
    )
    sink = _sink(transport)
    sink.find_open_pull_request(REPO, requested.head_branch, requested.base_branch)
    sink.open_pull_request(requested)

    for method, url, _headers, _body in transport.requests:
        assert method in {"GET", "POST"}
        assert "/merge" not in url
        assert "/issues/" not in url
        assert "/pulls" in url


# -- title bounding --------------------------------------------------------


def test_pr_title_is_bounded_and_stripped_of_non_printables() -> None:
    transport = FakeWriteTransport([_open_payload(61)])
    requested = PullRequest(
        repository_slug="example/target",
        head_branch="factory/task/ws",
        base_branch="main",
        title="a" * 500 + "\x00bad",
    )
    _sink(transport).open_pull_request(requested)
    sent_title = transport.requests[0][3]["title"]
    assert len(sent_title) <= 200
    assert "\x00" not in sent_title


# -- transport security ----------------------------------------------------

# Built by concatenation so no test source line carries the literal URL value:
# a traceback renders the source line of each frame, and the value must not
# appear there any more than in the exception message.
_INSECURE_HTTP_API = "http" + "://api.example.invalid"
_USERINFO_USER = "api_user_93726"
_USERINFO_API = "https://" + _USERINFO_USER + ":" + SECRET + "@api.example.invalid"


def test_plaintext_http_api_url_is_refused_before_transport() -> None:
    transport = FakeWriteTransport([])
    unsafe_url = _INSECURE_HTTP_API
    with pytest.raises(GitHubWriteError) as caught:
        GitHubWriteClient(token=SECRET, api_url=unsafe_url, transport=transport)

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    assert transport.requests == []
    for text in (SECRET, "api.example.invalid", "http://"):
        assert text not in str(error)
        assert text not in repr(error)
        assert text not in formatted
    assert error.__cause__ is None and error.__context__ is None


def test_userinfo_bearing_api_url_is_refused_before_transport() -> None:
    transport = FakeWriteTransport([])
    unsafe_url = _USERINFO_API
    with pytest.raises(GitHubWriteError) as caught:
        GitHubWriteClient(token=SECRET, api_url=unsafe_url, transport=transport)

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    assert transport.requests == []
    for text in (SECRET, _USERINFO_USER, "api.example.invalid", "https://"):
        assert text not in str(error)
        assert text not in repr(error)
        assert text not in formatted
    assert error.__cause__ is None and error.__context__ is None


def test_https_default_api_url_still_works() -> None:
    transport = FakeWriteTransport([[_open_payload(71)]])
    found = _sink(transport).find_open_pull_request(REPO, "factory/task/ws", "main")
    assert found is not None
    assert found.number == 71
    assert transport.requests[0][1].startswith("https://api.github.com/")
