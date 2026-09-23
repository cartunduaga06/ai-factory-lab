"""Minimal GitHub REST client.

The factory deliberately avoids a heavy GitHub SDK: it needs exactly one
read-only operation shape (``GET`` with query parameters), so a small wrapper
over the standard library is easier to audit and keeps the dependency surface
at zero.

The crucial property for security and testability is that the HTTP transport is
injectable. Production uses :class:`UrllibTransport`; tests supply an in-memory
fake and never touch the network. The token is held privately and is never
placed in a URL, a log message, an exception message or a ``repr``.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol


class GitHubError(RuntimeError):
    """Base class for GitHub integration failures."""


class GitHubAuthError(GitHubError):
    """The token is missing, invalid or lacks the required permission."""


class GitHubRequestError(GitHubError):
    """The API returned an unexpected error response."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(f"GitHub API request failed with status {status}: {message}")
        self.status = status


class Transport(Protocol):
    """Executes a single HTTP GET and returns the decoded JSON body."""

    def get_json(self, url: str, headers: Mapping[str, str]) -> Any:  # noqa: ANN401
        """Perform a GET, returning parsed JSON. Raises GitHubError on failure."""
        ...


class UrllibTransport:
    """Default transport, built on :mod:`urllib` from the standard library."""

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout

    def get_json(self, url: str, headers: Mapping[str, str]) -> Any:  # noqa: ANN401
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Never echo the request headers: the Authorization header carries
            # the token. Only the status and a bounded body excerpt are surfaced.
            raise _translate_http_error(exc) from None
        except urllib.error.URLError as exc:
            raise GitHubRequestError(0, f"network error: {exc.reason}") from None
        except json.JSONDecodeError:
            raise GitHubRequestError(0, "response body was not valid JSON") from None


def _translate_http_error(exc: urllib.error.HTTPError) -> GitHubError:
    detail = _safe_message_from_body(exc)
    if exc.code in (401, 403):
        return GitHubAuthError(f"GitHub authentication failed with status {exc.code}: {detail}")
    return GitHubRequestError(exc.code, detail)


def _safe_message_from_body(exc: urllib.error.HTTPError) -> str:
    try:
        raw = exc.read().decode("utf-8", errors="replace")
        payload = json.loads(raw)
    except (OSError, ValueError):
        return "no response body"
    message = payload.get("message") if isinstance(payload, dict) else None
    return str(message) if message else "no message"


class GitHubClient:
    """Read-only GitHub REST client scoped to a single API base URL."""

    _DEFAULT_ACCEPT = "application/vnd.github+json"

    def __init__(
        self,
        token: str,
        api_url: str,
        transport: Transport | None = None,
    ) -> None:
        self._token = token
        self._api_url = api_url.rstrip("/")
        self._transport = transport or UrllibTransport()

    def __repr__(self) -> str:
        # Deliberately omits the token; a repr must be safe to log.
        return f"GitHubClient(api_url={self._api_url!r})"

    def get(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:  # noqa: ANN401
        """GET ``path`` (relative to the API base) with optional query params."""
        url = f"{self._api_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return self._transport.get_json(url, self._headers())

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": self._DEFAULT_ACCEPT,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-factory-lab",
        }


__all__ = [
    "GitHubAuthError",
    "GitHubClient",
    "GitHubError",
    "GitHubRequestError",
    "Transport",
    "UrllibTransport",
]
