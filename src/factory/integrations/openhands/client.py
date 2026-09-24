"""Minimal client for the OpenHands Agent Server HTTP API.

The factory needs only a handful of conversation operations, so this is a small
``urllib`` wrapper rather than a dependency on the OpenHands SDK. That keeps the
dependency surface at zero and, more importantly, gives the factory a single
auditable boundary where responses are parsed and errors are sanitized.

Two properties matter here:

* **The HTTP transport is injectable.** Production uses :class:`UrllibTransport`;
  tests supply an in-memory fake and never touch the network.
* **Nothing raw escapes.** The session key lives in a private attribute and is
  never placed in a URL, a ``repr``, a log line, an exception message or a
  chained exception. Remote HTTP error-body text is discarded at this boundary
  rather than truncated or redacted: only trusted local data (the numeric HTTP
  status) is attached to an error.

Authentication (when the server requires it) uses the ``X-Session-API-Key``
header understood by OpenHands Agent Server v1.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Protocol, cast

CONVERSATIONS_PATH = "/api/conversations"

#: Longest sanitized detail attached to an error, so a pathological body cannot
#: turn an exception message into a payload dump.
MAX_DETAIL_CHARS = 200

#: Token shapes that must never survive into a message, even when the exact
#: credential value is not known to this client.
_TOKEN_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_\-]{4,}"),
    re.compile(r"gAAAA[A-Za-z0-9_\-=]{4,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{4,}"),
    re.compile(r"Bearer\s+\S+", re.IGNORECASE),
)


class OpenHandsError(RuntimeError):
    """Base class for every OpenHands integration failure.

    These are sanitized integration errors: they never carry a token, a request
    header, a raw response body, a credential-bearing URL or an LLM provider
    message.
    """


class OpenHandsConfigurationError(OpenHandsError):
    """The client was constructed with unusable configuration."""


class OpenHandsConnectionError(OpenHandsError):
    """The agent server could not be reached at the transport level."""

    def __init__(self) -> None:
        super().__init__("could not reach the OpenHands agent server")


class OpenHandsRequestError(OpenHandsError):
    """The agent server answered with an unexpected HTTP status.

    Built from trusted local data only: the numeric status. Remote error-body
    text (``detail``, ``message``, ``error``, ...) is discarded at the client
    boundary and never stored, echoed or chained, because the client cannot know
    every credential or sensitive value a server or LLM provider might echo.
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"OpenHands agent server returned status {status}")
        self.status = status


class OpenHandsResponseError(OpenHandsError):
    """The agent server returned a body the client could not interpret."""


class OpenHandsStatusError(OpenHandsError):
    """The agent server reported an execution status the factory cannot map.

    Raised rather than guessed: an unrecognized external status must never be
    silently normalized to success.

    The message is a fixed, sanitized constant. An unrecognized status is
    untrusted external input and could embed a token, credential, provider
    message or other sensitive payload, so the offending value is neither stored
    nor echoed, and no chained exception is retained.
    """

    def __init__(self) -> None:
        super().__init__("OpenHands reported an unrecognized execution status")


def redact(text: str, secrets: Iterable[str | None] = ()) -> str:
    """Remove every known secret and token shape from ``text``.

    Belt-and-braces: exact configured values are replaced, and a few common token
    prefixes are stripped even when the value is unknown to this process.
    """
    result = text
    for secret in secrets:
        if secret:
            result = result.replace(secret, "***")
    for pattern in _TOKEN_PATTERNS:
        result = pattern.sub("***", result)
    return result


def mask_url(url: str) -> str:
    """Mask any ``user:password@`` userinfo inside a URL, keeping the rest.

    A base URL is configuration, not a credential field, but it can still carry
    one, so it must not be logged verbatim.
    """
    scheme, sep, remainder = url.partition("://")
    if not sep or "@" not in remainder:
        return url
    _, _, host_part = remainder.partition("@")
    return f"{scheme}://***@{host_part}"


def bound(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    """Collapse whitespace and cap ``text`` at ``limit`` characters."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return f"{collapsed[:limit]}..."


@dataclass(slots=True, frozen=True)
class ServerResponse:
    """One HTTP response, already parsed.

    ``body`` is the decoded JSON when the response carried any, otherwise
    ``None``.
    """

    status: int
    body: object = None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300


class Transport(Protocol):
    """Executes a single HTTP request and returns the parsed response.

    Implementations must translate transport-level failures into
    :class:`OpenHandsConnectionError` without echoing the URL or any header.
    """

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> ServerResponse:
        """Perform one request. Raises :class:`OpenHandsConnectionError`."""
        ...


def _parse_json(raw: bytes) -> object:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


class UrllibTransport:
    """Default transport, built on :mod:`urllib` from the standard library."""

    def __init__(self, timeout: float = 30.0) -> None:
        self._timeout = timeout

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> ServerResponse:
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                return ServerResponse(status=response.status, body=_parse_json(response.read()))
        except urllib.error.HTTPError as exc:
            # HTTPError is also a URLError, so it must be caught first. The error
            # object holds the response body, which is returned for the client to
            # sanitize; nothing about it is attached to an exception here.
            try:
                payload = _parse_json(exc.read())
            except OSError:
                payload = None
            return ServerResponse(status=exc.code, body=payload)
        except (urllib.error.URLError, OSError):
            # Deliberately discards the reason (it can contain a host or a URL)
            # and raises without chaining, so no transport detail leaks.
            raise OpenHandsConnectionError() from None


def _as_mapping(value: object) -> Mapping[str, object] | None:
    if isinstance(value, Mapping):
        return cast("Mapping[str, object]", value)
    return None


class OpenHandsClient:
    """Client scoped to one OpenHands Agent Server base URL."""

    def __init__(
        self,
        base_url: str,
        *,
        session_api_key: str | None = None,
        transport: Transport | None = None,
        timeout: float = 30.0,
    ) -> None:
        cleaned = base_url.strip().rstrip("/")
        if not cleaned:
            raise OpenHandsConfigurationError("OpenHands base URL must not be empty")
        self._base_url = cleaned
        self._session_api_key = session_api_key
        self._transport = transport or UrllibTransport(timeout)

    def __repr__(self) -> str:
        # Deliberately omits the session key; a repr must be safe to log.
        return f"OpenHandsClient(base_url={mask_url(self._base_url)!r})"

    @property
    def base_url(self) -> str:
        """The configured base URL with any userinfo masked."""
        return mask_url(self._base_url)

    def secret_values(self) -> tuple[str, ...]:
        """Every credential this client holds, for redaction at the boundary."""
        return tuple(value for value in (self._session_api_key,) if value)

    def create_conversation(self, payload: Mapping[str, object]) -> Mapping[str, object]:
        """Start a conversation and return its server-side descriptor.

        Raises:
            OpenHandsRequestError: on an unexpected HTTP status.
            OpenHandsResponseError: if the response body is not a JSON object.
        """
        response = self._send("POST", CONVERSATIONS_PATH, payload)
        self._raise_for_status(response)
        body = _as_mapping(response.body)
        if body is None:
            raise OpenHandsResponseError("conversation creation returned no JSON object")
        return body

    def get_conversation(self, conversation_id: str) -> Mapping[str, object] | None:
        """Return the conversation descriptor, or ``None`` if it no longer exists."""
        response = self._send("GET", f"{CONVERSATIONS_PATH}/{conversation_id}")
        if response.status == 404:
            return None
        self._raise_for_status(response)
        body = _as_mapping(response.body)
        if body is None:
            raise OpenHandsResponseError("conversation lookup returned no JSON object")
        return body

    def interrupt_conversation(self, conversation_id: str) -> bool:
        """Request an immediate interrupt.

        Returns ``True`` when the request was delivered and ``False`` when the
        conversation is already gone (``404``), which callers treat as an
        already-cancelled execution. Any other failure is raised, never absorbed.
        """
        response = self._send("POST", f"{CONVERSATIONS_PATH}/{conversation_id}/interrupt")
        if response.status == 404:
            return False
        self._raise_for_status(response)
        return True

    def agent_final_response(self, conversation_id: str) -> str | None:
        """Return the agent's final response text, or ``None`` when there is none."""
        response = self._send("GET", f"{CONVERSATIONS_PATH}/{conversation_id}/agent_final_response")
        if response.status == 404:
            return None
        self._raise_for_status(response)
        body = _as_mapping(response.body)
        if body is None:
            return None
        text = body.get("response")
        if isinstance(text, str) and text.strip():
            return text
        return None

    # -- internals ---------------------------------------------------------

    def _send(
        self, method: str, path: str, payload: Mapping[str, object] | None = None
    ) -> ServerResponse:
        headers = {"Accept": "application/json"}
        body: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body = json.dumps(payload).encode("utf-8")
        if self._session_api_key:
            headers["X-Session-API-Key"] = self._session_api_key
        return self._transport.send(method, f"{self._base_url}{path}", headers, body)

    def _raise_for_status(self, response: ServerResponse) -> None:
        if response.ok:
            return
        # Only the trusted numeric status crosses the boundary. Any remote
        # error-body text is discarded here: it is untrusted and may embed a
        # credential the client cannot recognize.
        raise OpenHandsRequestError(response.status)


__all__ = [
    "CONVERSATIONS_PATH",
    "OpenHandsClient",
    "OpenHandsConfigurationError",
    "OpenHandsConnectionError",
    "OpenHandsError",
    "OpenHandsRequestError",
    "OpenHandsResponseError",
    "OpenHandsStatusError",
    "ServerResponse",
    "Transport",
    "UrllibTransport",
    "bound",
    "mask_url",
    "redact",
]
