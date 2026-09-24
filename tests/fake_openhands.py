"""A deterministic in-memory transport for OpenHands integration tests.

This is not a mock of the code under test: it is a real
:class:`~factory.integrations.openhands.client.Transport` implementation whose
responses are scripted. Tests assert against the requests it captured and the
responses it returned, so no OpenHands server and no network is involved.
"""

from __future__ import annotations

from collections.abc import Mapping

from factory.integrations.openhands.client import (
    OpenHandsConnectionError,
    ServerResponse,
)


class RecordedRequest:
    """One captured request, for assertions about what was sent."""

    def __init__(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> None:
        self.method = method
        self.url = url
        self.headers = dict(headers)
        self.body = body


class FakeTransport:
    """Scripted transport: returns queued responses in order.

    Falls back to ``default`` once the queue is exhausted, so a test only needs
    to script the interesting calls. ``raise_on_send`` simulates a transport
    failure such as an unreachable server.
    """

    def __init__(
        self,
        responses: list[ServerResponse] | None = None,
        *,
        default: ServerResponse | None = None,
        raise_on_send: bool = False,
    ) -> None:
        self._responses = list(responses or [])
        self._default = default or ServerResponse(status=404, body={"detail": "not found"})
        self._raise_on_send = raise_on_send
        self.requests: list[RecordedRequest] = []

    def send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str],
        body: bytes | None,
    ) -> ServerResponse:
        self.requests.append(RecordedRequest(method, url, headers, body))
        if self._raise_on_send:
            raise OpenHandsConnectionError()
        if self._responses:
            return self._responses.pop(0)
        return self._default

    @property
    def last(self) -> RecordedRequest:
        return self.requests[-1]


__all__ = ["FakeTransport", "RecordedRequest"]
