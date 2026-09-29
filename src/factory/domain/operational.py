"""Pure, deliberately narrow operational task contract.

The first capability only creates a disposable proof artifact. A GitHub issue
cannot grant itself host mutation authority by describing a command in prose.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from enum import StrEnum

from factory.domain.models import FactoryTask

_BLOCK = re.compile(r"```factory-operational\s*\n(.*?)\n```", re.DOTALL)
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{64}")
_PROOF_BYTES = b"factory-operational-proof\n"


class OperationalCapability(StrEnum):
    """Named host capabilities; naming a capability never grants it."""

    SCRATCH = "scratch"
    DATABASE_READONLY = "database_readonly"
    DOCKER_INSPECT = "docker_inspect"
    SERVICE_HEALTH = "service_health"
    BACKUP = "backup"


@dataclass(slots=True, frozen=True)
class OperationalPolicy:
    """Operator-owned allowlists. No issue declaration can change this policy."""

    enabled: frozenset[OperationalCapability] = frozenset({OperationalCapability.SCRATCH})
    hosts: frozenset[str] = frozenset()
    paths: frozenset[str] = frozenset()
    commands: frozenset[str] = frozenset()
    targets: frozenset[str] = frozenset()

    def permits(
        self,
        capability: OperationalCapability,
        *,
        host: str = "",
        path: str = "",
        command: str = "",
        target: str = "",
    ) -> bool:
        """Require explicit policy for every host operation; backup stays gated."""
        if capability is OperationalCapability.SCRATCH:
            return capability in self.enabled and not any((host, path, command, target))
        if capability is OperationalCapability.BACKUP or capability not in self.enabled:
            return False
        return (
            bool(host and path and command and target)
            and host in self.hosts
            and path in self.paths
            and command in self.commands
            and target in self.targets
        )


@dataclass(slots=True, frozen=True)
class ScratchArtifact:
    """An authorized file in a per-run scratch directory, with exact bytes."""

    name: str
    payload_hex: str
    sha256: str

    @property
    def size(self) -> int:
        return len(self.payload_hex) // 2


def parse_scratch_artifact(task: FactoryTask) -> ScratchArtifact:
    """Reject missing, ambiguous or non-scratch operational declarations."""
    return _parse_body(task.body)


def canonical_scratch_body(body: str) -> str:
    """Keep only a validated, non-secret declaration at the intake boundary."""
    artifact = _parse_body(body)
    declaration = {
        "mode": "scratch_artifact",
        "risk": "low",
        "artifact": artifact.name,
        "payload_hex": artifact.payload_hex,
        "sha256": artifact.sha256,
    }
    return "```factory-operational\n" + json.dumps(declaration, sort_keys=True) + "\n```"


def _parse_body(body: str) -> ScratchArtifact:
    blocks = _BLOCK.findall(body)
    if len(blocks) != 1:
        raise ValueError("exactly one factory-operational block is required")
    try:
        value = json.loads(blocks[0])
    except json.JSONDecodeError:
        raise ValueError("invalid operational declaration") from None
    if not isinstance(value, dict) or set(value) != {
        "mode",
        "risk",
        "artifact",
        "payload_hex",
        "sha256",
    }:
        raise ValueError("unsupported operational declaration")
    if value["mode"] != "scratch_artifact" or value["risk"] != "low":
        raise ValueError("operational risk or mode is not authorized")
    name, payload, digest = value["artifact"], value["payload_hex"], value["sha256"]
    if not isinstance(name, str) or not _NAME.fullmatch(name) or name in {".", ".."}:
        raise ValueError("artifact name is unsafe")
    if not isinstance(payload, str) or not 2 <= len(payload) <= 8192:
        raise ValueError("artifact payload must contain 1-4096 bytes")
    try:
        data = bytes.fromhex(payload)
    except ValueError:
        raise ValueError("artifact payload must be hexadecimal") from None
    if not data or len(data) * 2 != len(payload):
        raise ValueError("artifact payload must be canonical hex")
    if data != _PROOF_BYTES:
        raise ValueError("only the fixed non-secret proof payload is authorized")
    if not isinstance(digest, str) or not _SHA.fullmatch(digest):
        raise ValueError("artifact SHA-256 is invalid")
    if hashlib.sha256(data).hexdigest() != digest:
        raise ValueError("artifact SHA-256 does not match the declared payload")
    return ScratchArtifact(name, payload.lower(), digest)
