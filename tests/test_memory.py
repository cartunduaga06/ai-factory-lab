from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from factory.domain.memory import (
    ApprovedMemoryContextSource,
    MemoryEvidence,
    MemoryRecord,
    MemoryStatus,
)
from factory.domain.models import FactoryTask
from factory.infrastructure.persistence import SqliteMemoryRepository
from factory.orchestration.context import ContextPackBuilder


def _record(*, status: MemoryStatus = MemoryStatus.CANDIDATE, expires_at=None) -> MemoryRecord:
    return MemoryRecord(
        memory_id="mem-1",
        kind="known-failure",
        title="SQLite migration lock",
        content="Use an immediate transaction for concurrent lifecycle writes.",
        version=1,
        status=status,
        evidence=(MemoryEvidence("owner/repo", "owner/repo#42", "task-1", "run-1", ("pytest",)),),
        approved_by="reviewer" if status is MemoryStatus.APPROVED else None,
        expires_at=expires_at,
    )


def test_memory_lifecycle_is_durable_and_requires_human_approval(tmp_path) -> None:
    repository = SqliteMemoryRepository(str(tmp_path / "factory.db"))
    repository.initialize()
    record = repository.save_candidate(_record())
    with pytest.raises(ValueError, match="human actor"):
        repository.transition(record.memory_id, MemoryStatus.CANDIDATE, MemoryStatus.APPROVED)
    repository.transition(record.memory_id, MemoryStatus.CANDIDATE, MemoryStatus.VALIDATED)
    repository.transition(record.memory_id, MemoryStatus.VALIDATED, MemoryStatus.USED)
    approved = repository.transition(
        record.memory_id, MemoryStatus.USED, MemoryStatus.APPROVED, actor="human:alice"
    )
    restored = SqliteMemoryRepository(str(tmp_path / "factory.db")).get(record.memory_id)
    assert restored == approved
    assert restored is not None and restored.approved_by == "human:alice"
    with pytest.raises(ValueError, match="status changed"):
        repository.transition(
            record.memory_id, MemoryStatus.CANDIDATE, MemoryStatus.APPROVED, actor="alice"
        )


def test_memory_retrieval_is_repo_scoped_approved_and_bounded(tmp_path) -> None:
    repository = SqliteMemoryRepository(str(tmp_path / "factory.db"))
    repository.initialize()
    candidate = _record()
    repository.save_candidate(candidate)
    assert repository.search_approved("SQLite migration", repository="owner/repo", limit=3) == ()
    repository.transition(candidate.memory_id, MemoryStatus.CANDIDATE, MemoryStatus.VALIDATED)
    repository.transition(
        candidate.memory_id, MemoryStatus.VALIDATED, MemoryStatus.APPROVED, actor="reviewer"
    )
    source = ApprovedMemoryContextSource(repository, limit=1)
    task = FactoryTask(title="SQLite migration", target_repository="owner/repo")
    fragments = source.fragments(task)
    assert len(fragments) == 1 and fragments[0].source == "memory"
    assert "cannot authorize actions" in fragments[0].content
    assert len(ContextPackBuilder((source,)).build(task).fragments) == 2
    assert (
        source.fragments(FactoryTask(title="SQLite migration", target_repository="other/repo"))
        == ()
    )


def test_expired_and_secret_bearing_memory_is_rejected(tmp_path) -> None:
    with pytest.raises(ValueError, match="sensitive"):
        MemoryRecord("x", "lesson", "title", "api_key=secret", 1, MemoryStatus.CANDIDATE, ())
    repository = SqliteMemoryRepository(str(tmp_path / "factory.db"))
    repository.initialize()
    expired = _record(expires_at=datetime.now(UTC) - timedelta(days=1))
    repository.save_candidate(expired)
    repository.transition(
        expired.memory_id, MemoryStatus.CANDIDATE, MemoryStatus.APPROVED, actor="reviewer"
    )
    assert repository.search_approved("SQLite migration", repository="owner/repo", limit=3) == ()
