"""Write-capable GitHub REST client with a strict provider-error boundary.

Phase 5 is the first time the factory writes to GitHub (push a branch and open a
pull request), so this client is deliberately separate from the read-only
:class:`~factory.integrations.github.client.GitHubClient` and stricter than it:

* **Remote response bodies are untrusted.** A GitHub error body (``message``,
  ``error``, ``detail``, an arbitrary string) is *never* propagated into an
  exception. Only the trusted numeric HTTP status escapes, so no provider text
  can reach a log, a traceback or a persisted value.
* **TLS is mandatory.** The API base URL must be ``https://`` with no embedded
  userinfo. A plaintext ``http://`` target — or one that already carries
  credentials — is refused before any request, so the write credential can never
  cross an unencrypted transport. The rejection never names the URL.
* **No credential in the error surface.** The token is held privately and is
  never placed in a URL, a header value that is echoed, an exception message, a
  ``repr``, ``__cause__`` or ``__context__``.
* **Injectable transport.** Production uses :class:`UrllibWriteTransport`; tests
  supply an in-memory fake and never touch the network.

There is deliberately **no** merge, close or issue-mutation method: the factory
may open and look up pull requests, and nothing more.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol


class GitHubWriteError(RuntimeError):
    """A GitHub write request failed.

    Carries only the numeric HTTP status — never the response body, the request
    headers or the token. ``0`` means the request never produced a status (a
    transport or decode failure).
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"GitHub write request failed with status {status}")
        self.status = status


class InsecureWriteTargetError(GitHubWriteError):
    """The GitHub API base URL is not safe to authenticate a write request.

    Raised when the configured base URL is plaintext ``http://`` or carries
    embedded userinfo: the write credential must never cross an unencrypted
    transport, and the factory must not reuse a credential-bearing URL. The
    rejection happens before the transport is invoked, and the message names
    neither the URL, a username, a password nor the token.
    """

    def __init__(self) -> None:
        super().__init__(0)
        self.args = ("GitHub write refused: API base URL is not HTTPS",)


class WriteTransport(Protocol):
    """Executes a single JSON HTTP request and returns the decoded JSON body."""

    def request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> Any:  # noqa: ANN401
        """Perform ``method`` on ``url``, returning parsed JSON.

        Raises:
            GitHubWriteError: on any failure, carrying the status only.
        """
        ...


class UrllibWriteTransport:
    """Default write transport, built on :mod:`urllib`."""

    def __init__(self, timeout: float = 15.0) -> None:
        self._timeout = timeout

    def request_json(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: Mapping[str, Any] | None,
    ) -> Any:  # noqa: ANN401
        data = json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            # The error body is untrusted provider text and is deliberately not
            # read: only the status crosses this boundary.
            raise GitHubWriteError(exc.code) from None
        except urllib.error.URLError:
            raise GitHubWriteError(0) from None
        except OSError:
            raise GitHubWriteError(0) from None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise GitHubWriteError(0) from None


class GitHubWriteClient:
    """Write-capable GitHub REST client scoped to a single HTTPS API base URL.

    The base URL is validated eagerly and must be ``https://`` with no embedded
    userinfo: a write credential must never cross an unencrypted transport, and
    the client refuses rather than silently upgrading or reusing a
    credential-bearing URL. The rejection names neither the URL nor the token.
    """

    _DEFAULT_ACCEPT = "application/vnd.github+json"

    def __init__(
        self,
        token: str,
        api_url: str,
        transport: WriteTransport | None = None,
    ) -> None:
        normalized = api_url.strip().rstrip("/")
        if not _is_secure_https(normalized) or _has_embedded_credentials(normalized):
            # Refuse before the transport is created or ever called, so no
            # credential can be transmitted over an unsafe transport.
            raise InsecureWriteTargetError()
        self._token = token
        self._api_url = normalized
        self._transport = transport or UrllibWriteTransport()

    def __repr__(self) -> str:
        # Deliberately omits the token; a repr must be safe to log.
        return f"GitHubWriteClient(api_url={self._api_url!r})"

    def get(self, path: str, params: Mapping[str, str | int] | None = None) -> Any:  # noqa: ANN401
        """GET ``path`` (relative to the API base) with optional query params."""
        url = self._url(path, params)
        return self._call("GET", url, None)

    def post(self, path: str, payload: Mapping[str, Any]) -> Any:  # noqa: ANN401
        """POST ``payload`` to ``path`` (relative to the API base)."""
        url = self._url(path)
        return self._call("POST", url, payload)

    def _call(self, method: str, url: str, payload: Mapping[str, Any] | None) -> Any:  # noqa: ANN401
        failed = False
        try:
            return self._transport.request_json(method, url, self._headers(), payload)
        except GitHubWriteError:
            raise
        except Exception:  # noqa: BLE001 - normalize a transport that leaks text
            # A transport must translate its own failures, but the client does not
            # trust it to: an unexpected exception's message could embed a response
            # body or a credential, so it is discarded rather than inspected.
            failed = True
        if failed:
            # Raised outside the ``except`` block so the discarded exception is not
            # retained as ``__context__``. Chaining it — even with ``from None`` —
            # would keep the object alive on the error, and its message could leak
            # a credential through an exception report.
            raise GitHubWriteError(0)

    def _url(self, path: str, params: Mapping[str, str | int] | None = None) -> str:
        url = f"{self._api_url}/{path.lstrip('/')}"
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        return url

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": self._DEFAULT_ACCEPT,
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "ai-factory-lab",
        }


def _is_secure_https(url: str) -> bool:
    """Whether ``url`` is an ``https://`` URL (the only transport a write may use)."""
    return url.lower().startswith("https://")


def _has_embedded_credentials(url: str) -> bool:
    """Whether ``url`` carries userinfo (``scheme://user:pass@host``)."""
    scheme, sep, remainder = url.partition("://")
    if not sep:
        return False
    authority = remainder.split("/", 1)[0]
    return "@" in authority


__all__ = [
    "GitHubWriteClient",
    "GitHubWriteError",
    "InsecureWriteTargetError",
    "UrllibWriteTransport",
    "WriteTransport",
]
