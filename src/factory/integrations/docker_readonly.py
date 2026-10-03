"""Bounded Docker metadata observations over an injectable GET-only transport."""

from __future__ import annotations

import hashlib
import json
import re
import socket
import time
import urllib.parse
from collections.abc import Callable, Mapping

from factory.domain.ports import DockerInspector

MAX_RESPONSE_BYTES = 256 * 1024
MAX_EVIDENCE_BYTES = 4096
TIMEOUT_SECONDS = 2.0
MAX_HEALTH_LOGS = 5


class DockerReadTransport:
    """Single-method transport seam; implementations cannot request mutation verbs."""

    def get(self, path: str, timeout: float, limit: int) -> bytes:
        """Fetch one bounded response using the transport's fixed read operation."""
        raise NotImplementedError


class UnixSocketDockerTransport(DockerReadTransport):
    """GET-only transport through the Factory-owned read-only Docker proxy."""

    PROXY_SOCKET = "/run/ai-factory/docker-readonly.sock"

    def __init__(self) -> None:
        self._socket_path = self.PROXY_SOCKET

    def get(self, path: str, timeout: float, limit: int) -> bytes:
        if not path.startswith("/") or "?" in path or ".." in path or "\\" in path:
            raise ValueError("invalid Docker API path")
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(self._socket_path)
            sock.sendall(
                f"GET {path} HTTP/1.0\r\nHost: docker\r\nAccept: application/json\r\n\r\n".encode()
            )
            response = bytearray()
            while len(response) <= limit:
                chunk = sock.recv(min(8192, limit + 1 - len(response)))
                if not chunk:
                    break
                response.extend(chunk)
            if len(response) > limit:
                raise ValueError("Docker response limit exceeded")
        finally:
            sock.close()
        header, separator, body = bytes(response).partition(b"\r\n\r\n")
        first = header.splitlines()[0] if header else b""
        if not separator or not first.startswith(b"HTTP/1.") or b" 200 " not in first:
            raise ValueError("Docker inspection failed")
        if b"transfer-encoding: chunked" in header.lower():
            raise ValueError("unsupported Docker response encoding")
        if len(body) > limit:
            raise ValueError("Docker response limit exceeded")
        lengths = [
            line.split(b":", 1)[1].strip()
            for line in header.splitlines()[1:]
            if line.lower().startswith(b"content-length:")
        ]
        if lengths and (
            len(lengths) != 1 or not lengths[0].isdigit() or int(lengths[0]) != len(body)
        ):
            raise ValueError("invalid Docker response length")
        return body


class DockerReadonlyInspector(DockerInspector):
    """Expose only state, image identity, restart count and selected networking."""

    def __init__(
        self,
        targets: Mapping[str, str],
        *,
        transport: DockerReadTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._targets = dict(targets)
        self._transport = transport or UnixSocketDockerTransport()
        self._monotonic = monotonic

    def registered_container(self, target_id: str) -> str:
        return self._targets.get(target_id, "")

    def inspect(self, target_id: str) -> str:
        container = self._targets.get(target_id)
        if container is None:
            raise ValueError("Docker target is not registered")
        deadline = self._monotonic() + TIMEOUT_SECONDS
        try:
            encoded = urllib.parse.quote(container, safe="")
            raw = self._get(f"/containers/{encoded}/json", deadline)
            data = json.loads(raw)
            result = _sanitize_inspect(data, target_id)
            if self._monotonic() > deadline:
                raise ValueError("Docker inspection timed out")
            evidence = json.dumps(result, sort_keys=True, separators=(",", ":"))
            if len(evidence.encode()) > MAX_EVIDENCE_BYTES:
                raise ValueError("Docker evidence limit exceeded")
            return evidence
        except ValueError:
            raise
        except (OSError, TimeoutError, json.JSONDecodeError, TypeError):
            raise ValueError("Docker inspection failed") from None

    def _get(self, path: str, deadline: float) -> bytes:
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise ValueError("Docker inspection timed out")
        response = self._transport.get(path, min(remaining, TIMEOUT_SECONDS), MAX_RESPONSE_BYTES)
        if not isinstance(response, bytes) or len(response) > MAX_RESPONSE_BYTES:
            raise ValueError("Docker response limit exceeded")
        return response


def _sanitize_inspect(value: object, target_id: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid Docker inspection response")
    state, config, network = (
        value.get("State"),
        value.get("Config"),
        value.get("NetworkSettings"),
    )
    if not all(isinstance(item, dict) for item in (state, config, network)):
        raise ValueError("invalid Docker inspection response")
    assert isinstance(state, dict) and isinstance(config, dict) and isinstance(network, dict)
    image_id = value.get("Image")
    if not isinstance(image_id, str) or re.fullmatch(r"sha256:[a-fA-F0-9]{64}", image_id) is None:
        image_id = None
    status = state.get("Status")
    if status not in {"created", "restarting", "running", "removing", "paused", "exited", "dead"}:
        status = "unknown"
    health_block = state.get("Health")
    health = health_block.get("Status") if isinstance(health_block, dict) else "none"
    if health not in {"none", "starting", "healthy", "unhealthy"}:
        health = "none"
    restart_count = value.get("RestartCount")
    if not isinstance(restart_count, int) or not 0 <= restart_count <= 2**31 - 1:
        raise ValueError("invalid Docker restart count")
    ports: list[dict[str, object]] = []
    bindings = network.get("Ports")
    if isinstance(bindings, dict):
        for key, entries in sorted(bindings.items()):
            if (
                not isinstance(key, str)
                or re.fullmatch(r"[0-9]{1,5}/(?:tcp|udp|sctp)", key) is None
            ):
                continue
            port = int(key.split("/", 1)[0])
            if not 1 <= port <= 65535 or not isinstance(entries, list):
                continue
            for entry in entries[:4]:
                if isinstance(entry, dict):
                    public = entry.get("HostPort")
                    if isinstance(public, str) and public.isdecimal() and 1 <= int(public) <= 65535:
                        ports.append({"container": key, "host_port": int(public)})
    network_map = network.get("Networks")
    networks = (
        sorted(
            name[:64]
            for name in network_map
            if isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", name)
        )[:8]
        if isinstance(network_map, dict)
        else []
    )
    health_evidence: list[dict[str, object]] = []
    if isinstance(health_block, dict) and isinstance(health_block.get("Log"), list):
        for entry in health_block["Log"][-MAX_HEALTH_LOGS:]:
            if isinstance(entry, dict) and isinstance(entry.get("Output"), str):
                output = entry["Output"].encode("utf-8", "replace")
                code = entry.get("ExitCode")
                health_evidence.append(
                    {
                        "exit_code": code if isinstance(code, int) else None,
                        "output_sha256": hashlib.sha256(output).hexdigest(),
                        "output_bytes": min(len(output), 65535),
                    }
                )
    return {
        "target_id": target_id,
        "state": status,
        "health": health,
        "image_id": image_id,
        "image_reference": _image_reference(config.get("Image")),
        "restart_count": restart_count,
        "ports": ports[:16],
        "networks": networks,
        "health_evidence": health_evidence,
    }


def _image_reference(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or len(value) > 255
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/@-]*", value) is None
    ):
        return None
    if any(word in value.lower() for word in ("secret", "token", "password", "credential")):
        return None
    return value
