"""Offline acceptance for bounded read-only Docker inspection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.domain.enums import TaskKind, TaskStatus
from factory.domain.models import FactoryTask, TaskSource
from factory.domain.operational import (
    OperationalCapability,
    OperationalPolicy,
    canonical_operational_body,
    parse_docker_inspect,
)
from factory.infrastructure.config import FactoryConfig
from factory.integrations.docker_readonly import (
    DockerReadonlyInspector,
    DockerReadTransport,
    UnixSocketDockerTransport,
)
from tests.test_operational import _runtime


class FakeTransport(DockerReadTransport):
    def __init__(self, response: bytes) -> None:
        self.response = response
        self.paths: list[str] = []

    def get(self, path: str, timeout: float, limit: int) -> bytes:
        assert timeout <= 2.0 and limit <= 256 * 1024
        self.paths.append(path)
        return self.response


def _raw_inspect() -> bytes:
    return json.dumps(
        {
            "Id": "secret-container-id",
            "Config": {
                "Image": "registry.example/app:stable",
                "Env": ["DB_PASSWORD=top-secret"],
                "Labels": {"credential": "also-secret"},
            },
            "Image": "sha256:" + "a" * 64,
            "HostConfig": {"Binds": ["/run/secrets:/run/secrets"]},
            "State": {
                "Status": "running",
                "Health": {
                    "Status": "healthy",
                    "Log": [{"ExitCode": 0, "Output": "password=secret"}],
                },
            },
            "RestartCount": 3,
            "NetworkSettings": {
                "Ports": {"8080/tcp": [{"HostIp": "0.0.0.0", "HostPort": "18080"}]},
                "Networks": {"factory-net": {"IPAddress": "10.0.0.2"}},
            },
        }
    ).encode()


def _task() -> FactoryTask:
    return FactoryTask(
        title="Inspect approved service",
        target_repository="example/control",
        source=TaskSource("github", "example/control", 125),
        kind=TaskKind.OPERATIONAL,
        labels=("factory-ready", "factory-operational"),
        body='```factory-operational\n{"mode":"docker_inspect","target_id":"api"}\n```',
    )


def test_inspection_only_returns_sanitized_allowlisted_facts() -> None:
    transport = FakeTransport(_raw_inspect())
    inspector = DockerReadonlyInspector({"api": "factory-api"}, transport=transport)
    evidence = inspector.inspect("api")
    result = json.loads(evidence)
    assert result["state"] == "running"
    assert result["health"] == "healthy"
    assert result["restart_count"] == 3
    assert result["ports"] == [{"container": "8080/tcp", "host_port": 18080}]
    assert result["networks"] == ["factory-net"]
    assert '"output_sha256"' in evidence
    assert "secret" not in evidence and "Env" not in evidence and "Binds" not in evidence
    assert transport.paths == ["/containers/factory-api/json"]


def test_docker_issue_declaration_is_narrow_and_canonical() -> None:
    body = _task().body
    assert parse_docker_inspect(body) == "api"
    canonical = canonical_operational_body(body)
    assert parse_docker_inspect(canonical) == "api"
    assert canonical_operational_body(canonical) == canonical
    for extra in (
        {"command": "docker restart"},
        {"container": "other"},
        {"tail": 100},
    ):
        payload = {"mode": "docker_inspect", "target_id": "api", **extra}
        invalid = "```factory-operational\n" + json.dumps(payload) + "\n```"
        with pytest.raises(ValueError):
            canonical_operational_body(invalid)


def test_unknown_and_unapproved_targets_fail_closed(tmp_path: Path) -> None:
    inspector = DockerReadonlyInspector(
        {"api": "factory-api"}, transport=FakeTransport(_raw_inspect())
    )
    with pytest.raises(ValueError, match="registered"):
        inspector.inspect("other")
    task = _task()
    runtime, tasks, runs, _, _ = _runtime(tmp_path, task, "missing-codex")
    runtime._docker_inspector = inspector
    runtime._operational_policy = OperationalPolicy(
        enabled=frozenset({OperationalCapability.DOCKER_INSPECT}),
        hosts=frozenset({"local"}),
        paths=frozenset({"docker-engine-api"}),
        commands=frozenset({"inspect"}),
        targets=frozenset(),
    )
    result = runtime.run_once()
    assert result.outcome == "OPERATIONAL_POLICY_BLOCKED"
    assert tasks.get(task.task_id).status is TaskStatus.BLOCKED  # type: ignore[union-attr]
    assert runs.list_runs(task.task_id) == []


def test_approved_runtime_path_and_registry_redaction(tmp_path: Path) -> None:
    task = _task()
    runtime, tasks, runs, publisher, sink = _runtime(tmp_path, task, "missing-codex")
    runtime._docker_inspector = DockerReadonlyInspector(
        {"api": "factory-api"}, transport=FakeTransport(_raw_inspect())
    )
    runtime._operational_policy = OperationalPolicy(
        enabled=frozenset({OperationalCapability.DOCKER_INSPECT}),
        hosts=frozenset({"local"}),
        paths=frozenset({"docker-engine-api"}),
        commands=frozenset({"inspect"}),
        docker_targets=frozenset({"api"}),
    )
    result = runtime.run_once()
    assert result.outcome == "OPERATIONAL_DONE"
    assert tasks.get(task.task_id).status is TaskStatus.DONE  # type: ignore[union-attr]
    summary = runs.list_runs(task.task_id)[0].summary or ""
    assert "top-secret" not in summary and "secret-container-id" not in summary
    assert publisher.calls == sink.create_calls == 0
    config = FactoryConfig.from_env({"FACTORY_DOCKER_INSPECT_TARGETS": '{"api":"factory-api"}'})
    assert config.docker_inspect_targets == (("api", "factory-api"),)
    assert "factory-api" not in json.dumps(config.redacted())


@pytest.mark.parametrize("selector", ["../secret", "api; restart", "", "a" * 129])
def test_docker_target_registry_rejects_unsafe_selectors(selector: str) -> None:
    with pytest.raises(ValueError, match="FACTORY_DOCKER_INSPECT_TARGETS"):
        FactoryConfig.from_env({"FACTORY_DOCKER_INSPECT_TARGETS": json.dumps({"api": selector})})


def test_default_transport_uses_factory_readonly_proxy_only() -> None:
    transport = UnixSocketDockerTransport()
    assert transport._socket_path == "/run/ai-factory/docker-readonly.sock"  # noqa: SLF001
    assert Path(transport._socket_path).name != "docker.sock"  # noqa: SLF001


def test_transport_response_limit_is_enforced() -> None:
    transport = FakeTransport(b"x" * (256 * 1024 + 1))
    with pytest.raises(ValueError, match="limit"):
        DockerReadonlyInspector({"api": "factory-api"}, transport=transport).inspect("api")
