"""Offline coverage for bounded service health observations."""

from __future__ import annotations

import json
import ssl
from datetime import UTC, datetime
from typing import Any

import pytest

from factory.domain.operational import (
    OperationalCapability,
    OperationalPolicy,
    canonical_operational_body,
    parse_service_health,
)
from factory.infrastructure.config import FactoryConfig
from factory.integrations.service_health import MAX_RESPONSE_BYTES, ServiceHealthChecker
from tests.test_database_readonly import _task as database_task
from tests.test_operational import _runtime


class _Response:
    def __init__(self, status: int, body: bytes = b"secret payload") -> None:
        self.status = status
        self._body = body

    def read(self, amount: int) -> bytes:
        return self._body[:amount]


class _Connection:
    response: _Response
    calls: list[tuple[str, str]] = []

    def __init__(self, host: str, port: int, *, timeout: float, context: ssl.SSLContext) -> None:
        assert host == "health.example.test" and port == 443
        assert timeout <= 2.0
        assert isinstance(context, ssl.SSLContext)

    def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
        self.calls.append((method, path))

    def getresponse(self) -> _Response:
        return self.response

    def close(self) -> None:
        return None


def _result(
    monkeypatch: pytest.MonkeyPatch, status: int, body: bytes = b"private content"
) -> dict[str, Any]:
    _Connection.response = _Response(status, body)
    _Connection.calls = []
    monkeypatch.setattr(
        "factory.integrations.service_health.http.client.HTTPSConnection", _Connection
    )
    checker = ServiceHealthChecker(
        {"pilot": "https://health.example.test/status"},
        now=lambda: datetime(2026, 1, 2, tzinfo=UTC),
    )
    value = json.loads(checker.check("pilot"))
    assert _Connection.calls == [("GET", "/status")]
    assert "private content" not in json.dumps(value)
    return value


@pytest.mark.parametrize(
    ("http_status", "expected"),
    [(204, "HEALTHY"), (404, "WARNING"), (503, "CRITICAL"), (302, "UNKNOWN")],
)
def test_http_status_maps_to_deterministic_health(
    monkeypatch: pytest.MonkeyPatch, http_status: int, expected: str
) -> None:
    result = _result(monkeypatch, http_status)
    assert result == {
        "target_id": "pilot",
        "status": expected,
        "observed_at": "2026-01-02T00:00:00+00:00",
        "evidence": f"http_{http_status}",
    }


def test_unknown_target_response_limit_and_transport_error_are_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checker = ServiceHealthChecker({"pilot": "https://health.example.test/status"})
    assert json.loads(checker.check("other"))["evidence"] == "unapproved_target"
    too_large = _result(monkeypatch, 200, b"x" * (MAX_RESPONSE_BYTES + 1))
    assert too_large["status"] == "UNKNOWN"
    assert too_large["evidence"] == "response_limit"


@pytest.mark.parametrize(
    "declaration",
    [
        {"mode": "service_health", "target_id": "pilot", "url": "https://evil.test"},
        {"mode": "service_health", "target_id": "../pilot"},
        {"mode": "service_health", "target_id": "pilot", "path": "/admin"},
    ],
)
def test_service_declaration_only_accepts_target_id(declaration: dict[str, str]) -> None:
    body = "```factory-operational\n" + json.dumps(declaration) + "\n```"
    with pytest.raises(ValueError):
        parse_service_health(body)
    with pytest.raises(ValueError):
        canonical_operational_body(body)


def test_service_policy_is_separate_and_config_redacts_urls() -> None:
    policy = OperationalPolicy(
        enabled=frozenset({OperationalCapability.SERVICE_HEALTH}),
        hosts=frozenset({"https"}),
        paths=frozenset({"https://health.example.test/status"}),
        commands=frozenset({"get"}),
        targets=frozenset({"pilot"}),
    )
    assert policy.permits(
        OperationalCapability.SERVICE_HEALTH,
        host="https",
        path="https://health.example.test/status",
        command="get",
        target="pilot",
    )
    assert not policy.permits(
        OperationalCapability.DATABASE_READONLY,
        host="https",
        path="https://health.example.test/status",
        command="get",
        target="pilot",
    )
    config = FactoryConfig.from_env(
        {"FACTORY_SERVICE_HEALTH_TARGETS": '{"pilot":"https://health.example.test/status"}'}
    )
    assert config.service_health_targets == (("pilot", "https://health.example.test/status"),)
    assert "health.example.test" not in json.dumps(config.redacted())


@pytest.mark.parametrize(
    "url",
    [
        "http://health.example.test/status",
        "https://user:pass@health.example.test/status",
        "https://health.example.test/status?token=secret",
        "https://health.example.test/status#fragment",
        "https://health.example.test:444/status",
        "https://127.0.0.1/status",
    ],
)
def test_invalid_or_private_service_target_is_rejected(url: str) -> None:
    raw = json.dumps({"pilot": url})
    with pytest.raises(ValueError, match="FACTORY_SERVICE_HEALTH_TARGETS"):
        FactoryConfig.from_env({"FACTORY_SERVICE_HEALTH_TARGETS": raw})


def test_unapproved_service_task_blocks_before_agent_dispatch(tmp_path: Any) -> None:
    task = database_task({"mode": "service_health", "target_id": "unknown"})
    runtime, tasks, runs, publisher, sink = _runtime(tmp_path, task, "missing-codex")
    runtime._service_health_checker = ServiceHealthChecker(
        {"approved": "https://health.example.test/"}
    )
    runtime._operational_policy = OperationalPolicy(enabled=frozenset())
    result = runtime.run_once()
    assert result.outcome == "OPERATIONAL_POLICY_BLOCKED"
    assert tasks.get(task.task_id).status.value == "BLOCKED"  # type: ignore[union-attr]
    assert runs.list_runs(task.task_id) == []
    assert publisher.calls == sink.create_calls == 0
