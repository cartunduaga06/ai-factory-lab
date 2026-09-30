"""Canonical context identities and durable dispatch metadata."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factory.domain.context import ContextFragment, build_pack, content_digest, pack_from_metadata
from factory.domain.models import FactoryTask, TaskSource
from factory.integrations.codex.context_source import ApprovedSkillSource
from factory.integrations.context.repository import RepositoryContextSource
from factory.orchestration.context import ContextPackBuilder


def _fragment(name: str, content: str) -> ContextFragment:
    return ContextFragment("test", "note", name, "1", content_digest(content), content)


def test_pack_is_order_independent_deduplicated_and_verified() -> None:
    first, second = _fragment("a", "alpha"), _fragment("b", "beta")
    pack = build_pack((second, first, first), budget=9)
    assert pack == build_pack((first, second), budget=9)
    assert [item.id for item in pack.fragments] == ["a", "b"]
    assert pack_from_metadata(json.dumps(pack.metadata())).sha256 == pack.sha256
    assert "alpha" not in json.dumps(pack.metadata())
    with pytest.raises(ValueError, match="budget"):
        build_pack((first, second), budget=8)
    with pytest.raises(ValueError, match="conflicting"):
        build_pack((first, _fragment("a", "changed")))
    with pytest.raises(ValueError, match="invalid context content"):
        build_pack((ContextFragment("test", "note", "x", "1", "bad", "content"),))


def test_rework_preserves_base_and_versions_feedback() -> None:
    task = FactoryTask(title="Example", target_repository="owner/repo", body="Implement")
    builder = ContextPackBuilder()
    base = builder.build(task)
    first = builder.build(task, feedback="Change one", previous=base)
    second = builder.build(task, feedback="Change two", previous=first)
    assert first.base_sha256 == base.sha256 == second.base_sha256
    assert first.sha256 != second.sha256
    assert first.fragments[0].id == base.sha256
    with pytest.raises(ValueError, match="historical"):
        builder.build(
            FactoryTask(title="Other", target_repository="owner/repo"),
            feedback="Change",
            previous=base,
        )


def test_issue_tail_changes_pack_identity_but_not_bounded_render() -> None:
    task = FactoryTask(title="Long issue", target_repository="owner/repo", body="x" * 8000 + "A")
    builder = ContextPackBuilder()
    first = builder.build(task)
    task.body = "x" * 8000 + "B"
    second = builder.build(task)
    assert first.render() == second.render()
    assert first.fragments[0].version != second.fragments[0].version
    assert first.sha256 != second.sha256


def test_same_issue_has_same_pack_across_local_task_ids() -> None:
    source = TaskSource("github", "owner/control", 69)
    first = FactoryTask(title="Issue", target_repository="owner/repo", body="Text", source=source)
    second = FactoryTask(title="Issue", target_repository="owner/repo", body="Text", source=source)
    assert first.task_id != second.task_id
    assert ContextPackBuilder().build(first).sha256 == ContextPackBuilder().build(second).sha256


def test_repository_source_has_bounded_content_and_full_file_identity(tmp_path: Path) -> None:
    (tmp_path / "AGENTS.md").write_text("Rules\n")
    (tmp_path / "src").mkdir()
    code = tmp_path / "src" / "feature.py"
    code.write_text("x" * 8000 + "A")
    task = FactoryTask(title="Edit src/feature.py", target_repository="owner/repo")
    builder = ContextPackBuilder((RepositoryContextSource(str(tmp_path)),))
    first = builder.build(task)
    assert [item.id for item in first.fragments if item.source == "repository"] == [
        "AGENTS.md",
        "src/feature.py",
    ]
    code.write_text("x" * 8000 + "B")
    second = builder.build(task)
    assert first.render() == second.render()
    assert first.sha256 != second.sha256


def test_approved_skill_enters_neutral_pack() -> None:
    task = FactoryTask(title="Example [type:verification]", target_repository="owner/repo")
    pack = ContextPackBuilder((ApprovedSkillSource("auto"),)).build(task)
    assert {item.source for item in pack.fragments} == {"factory", "ecc"}
    assert "Verification Loop Skill" in pack.render()
    with pytest.raises(ValueError):
        ContextPackBuilder((ApprovedSkillSource("unsupported"),)).build(task)


def test_pack_metadata_is_durable_and_audited_without_content(tmp_path: Path) -> None:
    from factory.domain.enums import AgentKind, RunStatus, TaskStatus
    from factory.domain.models import AgentRun
    from factory.infrastructure.persistence import SqliteRunRepository, SqliteTaskRepository
    from factory.infrastructure.persistence.audit import SqliteAuditEventStore

    database = str(tmp_path / "factory.db")
    tasks, runs = SqliteTaskRepository(database), SqliteRunRepository(database)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(
        FactoryTask(
            title="Context audit",
            target_repository="owner/repo",
            body="private issue text",
            status=TaskStatus.READY,
        )
    )
    pack = ContextPackBuilder().build(task)
    run = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.CODEX,
            status=RunStatus.FAILED,
            context_pack=pack,
        )
    )
    restored = runs.get_run(run.run_id)
    assert restored is not None and restored.context_pack == pack
    assert restored.context_pack.sha256 == pack.sha256
    assert [event.name for event in SqliteAuditEventStore(database).for_task(task.task_id)].count(
        "ContextPackBuilt"
    ) == 1
    assert all(fragment.content == "" for fragment in restored.context_pack.fragments)
