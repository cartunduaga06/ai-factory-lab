"""GitHub integration.

Read-only in Phase 2A for issue intake; Phase 5 adds a strictly bounded
write path for pull requests (push a branch, open a PR). The factory still never
merges, closes or otherwise mutates an Issue.
"""

from factory.integrations.github.client import (
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubRequestError,
    Transport,
    UrllibTransport,
)
from factory.integrations.github.issues import (
    ELIGIBILITY_LABEL,
    GITHUB_PROVIDER,
    GitHubIssueSource,
)
from factory.integrations.github.pull_requests import GitHubPullRequestSink
from factory.integrations.github.write_client import (
    GitHubWriteClient,
    GitHubWriteError,
    UrllibWriteTransport,
    WriteTransport,
)

__all__ = [
    "ELIGIBILITY_LABEL",
    "GITHUB_PROVIDER",
    "GitHubAuthError",
    "GitHubClient",
    "GitHubError",
    "GitHubIssueSource",
    "GitHubPullRequestSink",
    "GitHubRequestError",
    "GitHubWriteClient",
    "GitHubWriteError",
    "Transport",
    "UrllibTransport",
    "UrllibWriteTransport",
    "WriteTransport",
]
