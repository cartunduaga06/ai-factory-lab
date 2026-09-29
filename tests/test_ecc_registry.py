"""Registry boundaries and automatic skill selection."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from factory.domain.enums import TaskKind
from factory.domain.models import FactoryTask
from factory.integrations.codex.skill_registry import (
    REGISTRY_PATH,
    load_registered_skill,
    load_registry,
    parse_registry,
    select_skill,
)


def _task(title: str, *, kind: TaskKind = TaskKind.CODE) -> FactoryTask:
    return FactoryTask(title=title, target_repository="owner/repo", kind=kind)


def test_pinned_registry_parses_and_selects_explicit_type() -> None:
    records = load_registry()
    assert len(records) == 1
    selected = select_skill(_task("Review change [type:verification]"), records)
    assert selected.name == "verification-loop"
    assert selected.upstream_commit == "c9148d0bb239ed01a95724a5928b98cdf9c30658"
    assert "# Verification Loop Skill" in load_registered_skill(selected)


def test_registry_rejects_bad_schema_and_duplicate_names() -> None:
    data = json.loads(REGISTRY_PATH.read_text())
    data["version"] = 2
    with pytest.raises(ValueError, match="version"):
        parse_registry(json.dumps(data).encode())
    data["version"] = 1
    data["skills"].append(data["skills"][0])
    with pytest.raises(ValueError, match="duplicate"):
        parse_registry(json.dumps(data).encode())
    data["skills"].pop()
    data["skills"][0]["capabilities"] = ["shell"]
    with pytest.raises(ValueError, match="capability"):
        parse_registry(json.dumps(data).encode())


def test_registry_digest_and_regular_file_checks(tmp_path: Path) -> None:
    changed = tmp_path / "registry.json"
    changed.write_bytes(REGISTRY_PATH.read_bytes() + b" ")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_registry(path=changed)
    link = tmp_path / "link"
    link.symlink_to(REGISTRY_PATH)
    with pytest.raises(ValueError, match="regular file"):
        load_registry(path=link)


def test_selection_rejects_unknown_blocked_and_ambiguous() -> None:
    records = load_registry()
    with pytest.raises(ValueError, match="unknown"):
        select_skill(_task("Fix bug [type:bugfix]"), records)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        select_skill(_task("[type:verification] [type:verification]"), records)
    with pytest.raises(ValueError, match="missing or ambiguous"):
        select_skill(_task("[type:verification] [type:Unknown]"), records)
    with pytest.raises(ValueError, match="code task"):
        select_skill(_task("[type:verification]", kind=TaskKind.OPERATIONAL), records)
    blocked = replace(records[0], approval_status="BLOCKED")
    with pytest.raises(ValueError, match="blocked"):
        select_skill(_task("[type:verification]"), (blocked,))
    duplicate_type = replace(records[0], name="second-skill")
    with pytest.raises(ValueError, match="ambiguous"):
        select_skill(_task("[type:verification]"), (records[0], duplicate_type))


def test_selected_skill_digest_mismatch_and_symlink(tmp_path: Path) -> None:
    selected = load_registry()[0]
    with pytest.raises(ValueError, match="unregistered"):
        load_registered_skill(replace(selected, name="unknown"))
    location = tmp_path / selected.name
    location.mkdir()
    skill = location / "SKILL.md"
    skill.write_text("changed")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_registered_skill(selected, root=tmp_path)
    skill.unlink()
    skill.symlink_to(REGISTRY_PATH)
    with pytest.raises(ValueError, match="regular file"):
        load_registered_skill(selected, root=tmp_path)
    skill.unlink()
    skill.write_bytes(REGISTRY_PATH.read_bytes())
    assert hashlib.sha256(skill.read_bytes()).hexdigest() != selected.sha256
