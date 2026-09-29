"""Factory-owned allowlist for pinned ECC guidance; no upstream runtime access."""

from __future__ import annotations

import hashlib
import json
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from factory.domain.enums import TaskKind
from factory.domain.models import FactoryTask

REGISTRY_PATH = Path(__file__).parent / "ecc" / "registry.json"
REGISTRY_SHA256 = "61de28f0352632cfb80d69add1a59e5e98b62a6353df691c90824a23efc5feef"
_TASK_TYPE = re.compile(r"\[type:([a-z][a-z0-9-]*)\]")
_NAME = re.compile(r"[a-z][a-z0-9-]*")
_SHA = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class SkillRecord:
    """Provenance and allowed use of one reviewed instruction file."""

    name: str
    upstream_repository: str
    upstream_commit: str
    source_path: str
    sha256: str
    task_types: tuple[str, ...]
    capabilities: tuple[str, ...]
    approval_status: str


def _read_regular(path: Path) -> bytes:
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError("ECC file is not a regular file")
        return path.read_bytes()
    except OSError as exc:
        raise ValueError("ECC file is unavailable") from exc


def parse_registry(content: bytes) -> tuple[SkillRecord, ...]:
    """Validate the complete versioned registry schema without permissive defaults."""
    try:
        data = json.loads(content)
        if not isinstance(data, dict) or set(data) != {"version", "skills"}:
            raise ValueError("invalid ECC registry shape")
        if type(data["version"]) is not int or data["version"] != 1:
            raise ValueError("unsupported ECC registry version")
        entries = data["skills"]
        if not isinstance(entries, list) or not 3 <= len(entries) <= 5:
            raise ValueError("ECC registry must contain 3 to 5 skills")
        records: list[SkillRecord] = []
        for entry in entries:
            if not isinstance(entry, dict) or set(entry) != set(SkillRecord.__dataclass_fields__):
                raise ValueError("invalid ECC skill record")
            strings = (
                "name",
                "upstream_repository",
                "upstream_commit",
                "source_path",
                "sha256",
                "approval_status",
            )
            if any(not isinstance(entry[key], str) for key in strings):
                raise ValueError("invalid ECC skill metadata")
            name = entry["name"]
            if not _NAME.fullmatch(name):
                raise ValueError("invalid ECC skill name")
            if entry["upstream_repository"] != "https://github.com/affaan-m/ECC":
                raise ValueError("unsupported ECC upstream")
            if not _COMMIT.fullmatch(entry["upstream_commit"]):
                raise ValueError("invalid ECC commit")
            if entry["source_path"] != f"skills/{name}/SKILL.md":
                raise ValueError("invalid ECC source path")
            if not _SHA.fullmatch(entry["sha256"]):
                raise ValueError("invalid ECC digest")
            if entry["approval_status"] not in {"APPROVED", "BLOCKED"}:
                raise ValueError("invalid ECC approval")
            for field in ("task_types", "capabilities"):
                values = entry[field]
                if (
                    not isinstance(values, list)
                    or not values
                    or any(
                        not isinstance(value, str) or not _NAME.fullmatch(value) for value in values
                    )
                    or len(values) != len(set(values))
                ):
                    raise ValueError("invalid ECC allowlist")
            if entry["capabilities"] != ["code-guidance"]:
                raise ValueError("unsupported ECC capability")
            records.append(
                SkillRecord(
                    name=name,
                    upstream_repository=entry["upstream_repository"],
                    upstream_commit=entry["upstream_commit"],
                    source_path=entry["source_path"],
                    sha256=entry["sha256"],
                    task_types=tuple(entry["task_types"]),
                    capabilities=tuple(entry["capabilities"]),
                    approval_status=entry["approval_status"],
                )
            )
        if len({record.name for record in records}) != len(records):
            raise ValueError("duplicate ECC skill")
        return tuple(records)
    except (UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ValueError("invalid ECC registry") from exc


def load_registry(
    *, path: Path = REGISTRY_PATH, digest: str = REGISTRY_SHA256
) -> tuple[SkillRecord, ...]:
    """Read only the exact reviewed registry bytes from a regular file."""
    content = _read_regular(path)
    if hashlib.sha256(content).hexdigest() != digest:
        raise ValueError("ECC registry digest mismatch")
    return parse_registry(content)


def select_skill(task: FactoryTask, records: tuple[SkillRecord, ...]) -> SkillRecord:
    """Select exactly one approved code skill from an explicit issue title tag."""
    if task.kind is not TaskKind.CODE:
        raise ValueError("ECC skills require a code task")
    task_types = _TASK_TYPE.findall(task.title)
    if len(task_types) != 1 or task.title.count("[type:") != 1:
        raise ValueError("ECC task type missing or ambiguous")
    matches = [record for record in records if task_types[0] in record.task_types]
    if len(matches) != 1:
        raise ValueError("ECC skill selection unknown or ambiguous")
    selected = matches[0]
    if selected.approval_status != "APPROVED" or selected.capabilities != ("code-guidance",):
        raise ValueError("ECC skill is blocked")
    return selected


def load_registered_skill(record: SkillRecord, *, root: Path = REGISTRY_PATH.parent) -> str:
    """Load approved prose only after checking the vendored file's exact digest."""
    if record not in load_registry() or record.approval_status != "APPROVED":
        raise ValueError("ECC skill is unregistered or blocked")
    content = _read_regular(root / record.name / "SKILL.md")
    if hashlib.sha256(content).hexdigest() != record.sha256:
        raise ValueError("ECC skill digest mismatch")
    return content.decode("utf-8")
