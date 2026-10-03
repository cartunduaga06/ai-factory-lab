"""Bounded read-only HTTPS probes over operator-registered health targets."""

from __future__ import annotations

import http.client
import ipaddress
import json
import ssl
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from urllib.parse import SplitResult, urlsplit

from factory.domain.ports import ServiceHealthChecker as ServiceHealthCheckerPort

MAX_RESPONSE_BYTES = 4096
TIMEOUT_SECONDS = 2.0


class ServiceHealthChecker(ServiceHealthCheckerPort):
    """Issue fixed GET probes and return only bounded, sanitized health evidence."""

    def __init__(
        self,
        targets: Mapping[str, str],
        *,
        timeout: float = TIMEOUT_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not 0 < timeout <= TIMEOUT_SECONDS:
            raise ValueError("service health timeout exceeds policy")
        for url in targets.values():
            _validate_target(url)
        self._targets = dict(targets)
        self._timeout = timeout
        self._monotonic = monotonic
        self._now = now

    def target_url(self, target_id: str) -> str:
        """Return the registered URL only for exact policy matching."""
        return self._targets.get(target_id, "")

    def check(self, target_id: str) -> str:
        """Observe one exact HTTPS target; failures reveal no response or URL data."""
        url = self._targets.get(target_id)
        timestamp = self._now().astimezone(UTC).isoformat()
        if url is None:
            return _evidence(target_id, "UNKNOWN", timestamp, "unapproved_target")
        try:
            parsed = _validate_target(url)
        except ValueError:
            return _evidence(target_id, "UNKNOWN", timestamp, "unsupported_target")
        host = parsed.hostname
        if host is None:
            return _evidence(target_id, "UNKNOWN", timestamp, "unsupported_target")
        deadline = self._monotonic() + self._timeout
        connection: http.client.HTTPSConnection | None = None
        try:
            connection = http.client.HTTPSConnection(
                host,
                parsed.port or 443,
                timeout=min(self._timeout, max(0.001, deadline - self._monotonic())),
                context=ssl.create_default_context(),
            )
            path = parsed.path or "/"
            connection.request(
                "GET", path, headers={"Accept": "application/json", "Connection": "close"}
            )
            if self._monotonic() >= deadline:
                return _evidence(target_id, "UNKNOWN", timestamp, "timeout")
            response = connection.getresponse()
            body = response.read(MAX_RESPONSE_BYTES + 1)
            if len(body) > MAX_RESPONSE_BYTES:
                return _evidence(target_id, "UNKNOWN", timestamp, "response_limit")
            if self._monotonic() >= deadline:
                return _evidence(target_id, "UNKNOWN", timestamp, "timeout")
            status = response.status
            if 200 <= status < 300:
                state = "HEALTHY"
            elif 400 <= status < 500:
                state = "WARNING"
            elif 500 <= status < 600:
                state = "CRITICAL"
            else:
                state = "UNKNOWN"
            return _evidence(target_id, state, timestamp, f"http_{status}")
        except (TimeoutError, OSError, http.client.HTTPException, ssl.SSLError):
            return _evidence(target_id, "UNKNOWN", timestamp, "probe_failed")
        finally:
            if connection is not None:
                connection.close()


def _validate_target(url: str) -> SplitResult:
    """Reject targets that can address a private or ambiguous network location."""
    parsed = urlsplit(url)
    host = parsed.hostname
    try:
        address = ipaddress.ip_address(host or "")
    except ValueError:
        address = None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
        or "\\" in url
        or (address is not None and not address.is_global)
        or host.lower() == "localhost"
        or host.lower().endswith((".localhost", ".local"))
    ):
        raise ValueError("unsupported service target")
    return parsed


def _evidence(target_id: str, state: str, timestamp: str, detail: str) -> str:
    return json.dumps(
        {"target_id": target_id, "status": state, "observed_at": timestamp, "evidence": detail},
        sort_keys=True,
        separators=(",", ":"),
    )
