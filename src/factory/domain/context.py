"""Engine-neutral, versioned context identities and canonical pack construction."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Protocol

from factory.domain.models import FactoryTask


@dataclass(frozen=True, slots=True)
class ContextFragment:
    source: str
    type: str
    id: str
    version: str
    content_ref: str
    content: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if not all((self.source, self.type, self.id, self.version, self.content_ref)):
            raise ValueError("incomplete context fragment")

    def metadata(self) -> dict[str, str]:
        return {
            "source": self.source,
            "type": self.type,
            "id": self.id,
            "version": self.version,
            "content_ref": self.content_ref,
        }


@dataclass(frozen=True, slots=True)
class ContextPack:
    version: int
    budget: int
    fragments: tuple[ContextFragment, ...]
    sha256: str
    base_sha256: str | None = None

    def metadata(self) -> dict[str, object]:
        return {
            "version": self.version,
            "budget": self.budget,
            "fragments": [fragment.metadata() for fragment in self.fragments],
            "sha256": self.sha256,
            "base_sha256": self.base_sha256,
        }

    def render(self) -> str:
        if any(not fragment.content for fragment in self.fragments):
            raise ValueError("context content unavailable")
        return "\n\n".join(fragment.content for fragment in self.fragments)


class ContextSource(Protocol):
    """Supply independently verified fragments for a task."""

    def fragments(self, task: FactoryTask) -> tuple[ContextFragment, ...]: ...


def content_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def build_pack(
    fragments: tuple[ContextFragment, ...],
    *,
    budget: int = 48_000,
    base_sha256: str | None = None,
) -> ContextPack:
    """Validate, deduplicate, order, and hash exact fragment identities."""
    if budget < 1:
        raise ValueError("invalid context budget")
    unique: dict[tuple[str, str, str], ContextFragment] = {}
    for fragment in fragments:
        if not fragment.content or content_digest(fragment.content) != fragment.content_ref:
            raise ValueError("invalid context content")
        key = (fragment.source, fragment.type, fragment.id)
        if key in unique and unique[key] != fragment:
            raise ValueError("conflicting context fragment")
        unique[key] = fragment
    ordered = tuple(unique[key] for key in sorted(unique))
    if not ordered or sum(len(item.content) for item in ordered) > budget:
        raise ValueError("required context missing or over budget")
    canonical = {
        "version": 1,
        "budget": budget,
        "fragments": [item.metadata() for item in ordered],
        "base_sha256": base_sha256,
    }
    digest = content_digest(json.dumps(canonical, sort_keys=True, separators=(",", ":")))
    return ContextPack(1, budget, ordered, digest, base_sha256)


def pack_from_metadata(raw: str) -> ContextPack:
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "budget", "fragments", "sha256", "base_sha256"}
        or data["version"] != 1
    ):
        raise ValueError("invalid stored context pack")
    fragments = tuple(ContextFragment(**item) for item in data["fragments"])
    canonical = {key: data[key] for key in ("version", "budget", "fragments", "base_sha256")}
    digest = content_digest(json.dumps(canonical, sort_keys=True, separators=(",", ":")))
    if digest != data["sha256"]:
        raise ValueError("context pack digest mismatch")
    return ContextPack(1, data["budget"], fragments, digest, data["base_sha256"])
