"""GitHub integration.

Read-only in Phase 2A: the factory observes Issues and does not yet control the
GitHub issue lifecycle (no labelling, commenting, closing or editing).
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

__all__ = [
    "ELIGIBILITY_LABEL",
    "GITHUB_PROVIDER",
    "GitHubAuthError",
    "GitHubClient",
    "GitHubError",
    "GitHubIssueSource",
    "GitHubRequestError",
    "Transport",
    "UrllibTransport",
]
