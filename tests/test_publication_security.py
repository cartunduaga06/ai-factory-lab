"""Phase 5 security tests: arbitrary external values must never escape.

The secret used here is an arbitrary passphrase, not a value that matches a known
token pattern, so a test can only pass if the boundary discards the value
structurally rather than pattern-matching it away.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, RepositoryRole, RunStatus
from factory.domain.errors import PublicationError
from factory.domain.models import AgentRun, FactoryTask, PullRequest, Workspace
from factory.integrations.github.pull_requests import GitHubPullRequestSink
from factory.integrations.github.write_client import GitHubWriteClient, GitHubWriteError
from factory.integrations.workspace.git_publish import GitWorkspacePublisher

# Arbitrary secret: not a GitHub token shape, not a password pattern.
SECRET = "MY_PRIVATE_PUSH_PASSWORD_93726"


def _assert_secret_absent(error: BaseException) -> None:
    formatted = "".join(traceback.format_exception(error))
    for surface in (str(error), repr(error), formatted):
        assert SECRET not in surface
    assert error.__cause__ is None
    assert error.__context__ is None


# -- git boundary ----------------------------------------------------------


def test_git_failure_hides_the_secret(tmp_path: Path) -> None:
    workspace = Workspace(
        repository_slug="example/target",
        branch="factory/task-1/ws-1",
        path=str(tmp_path / "ws"),
    )
    Path(workspace.path).mkdir(parents=True)
    run = AgentRun(
        task_id="task-1",
        adapter=AgentKind.OTHER,
        run_id="run-1",
        status=RunStatus.SUCCEEDED,
        workspace=workspace,
    )
    # The write token is set, but the workspace is not a git checkout, so
    # publication fails before any push. The token must not surface.
    publisher = GitWorkspacePublisher(write_token=SECRET)

    with pytest.raises(PublicationError) as caught:
        publisher.publish(FactoryTask(title="t", target_repository="example/target"), run)

    _assert_secret_absent(caught.value)
    assert SECRET not in repr(publisher)


def test_git_publisher_repr_never_carries_the_token(tmp_path: Path) -> None:
    publisher = GitWorkspacePublisher(write_token=SECRET)
    assert SECRET not in repr(publisher)


# -- GitHub boundary -------------------------------------------------------


class _LeakyTransport:
    """A transport that raises a raw exception whose message embeds the secret."""

    def request_json(self, method: str, url: str, headers: object, body: object) -> object:  # noqa: ANN401
        raise RuntimeError(f"remote said: {SECRET}")


def test_github_transport_leak_is_normalized() -> None:
    client = GitHubWriteClient(
        token=SECRET, api_url="https://api.github.com", transport=_LeakyTransport()
    )
    with pytest.raises(GitHubWriteError) as caught:
        client.get("/repos/example/target/pulls")
    assert caught.value.status == 0
    _assert_secret_absent(caught.value)
    assert SECRET not in repr(client)


def test_github_sink_error_never_carries_the_secret() -> None:
    sink = GitHubPullRequestSink(
        GitHubWriteClient(
            token=SECRET, api_url="https://api.github.com", transport=_LeakyTransport()
        )
    )
    with pytest.raises(PublicationError) as caught:
        sink.find_open_pull_request(_repository(), "factory/task-1/ws-1")
    _assert_secret_absent(caught.value)
    assert SECRET not in repr(sink)


def _repository() -> object:
    from factory.domain.models import Repository

    return Repository(slug="example/target", role=RepositoryRole.TARGET)


# -- forbidden capability --------------------------------------------------


def test_no_auto_merge_capability_exists() -> None:
    # Neither the domain model, the sink contract nor the GitHub client exposes a
    # merge operation: the factory can only propose, never merge.
    from factory.domain import ports

    assert not hasattr(PullRequest, "merge")
    assert not hasattr(ports.PullRequestSink, "merge_pull_request")
    assert not hasattr(GitHubWriteClient, "merge")
    assert not hasattr(GitHubWriteClient, "put")
    assert not hasattr(GitHubWriteClient, "merge_pull_request")
