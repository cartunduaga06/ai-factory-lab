"""SQLite persistence for post-rebase exact-head revalidation evidence."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

from factory.domain.enums import QualityGateStatus
from factory.domain.models import QualityGate
from factory.domain.ports import PostRebaseRevalidationRepository
from factory.domain.revalidation import PostRebaseRevalidation, RevalidationResult
from factory.domain.security import SecurityFinding, SecurityReview
from factory.infrastructure.persistence.codec import decode_datetime, encode_datetime
from factory.infrastructure.persistence.schema import POST_REBASE_REVALIDATIONS_TABLE
from factory.infrastructure.persistence.sqlite_base import SqliteRepository


class SqlitePostRebaseRevalidationRepository(SqliteRepository, PostRebaseRevalidationRepository):
    """Durable single-active-attempt storage with compare-and-swap updates."""

    def active_for_task(self, task_id: str) -> PostRebaseRevalidation | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} "
                "WHERE task_id = ? AND result IN ('PENDING', 'WAITING_CI') "
                "ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return _row(row) if row is not None else None

    def latest_for_task(self, task_id: str) -> PostRebaseRevalidation | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} "
                "WHERE task_id = ? ORDER BY started_at DESC, rowid DESC LIMIT 1",
                (task_id,),
            ).fetchone()
        return _row(row) if row is not None else None

    def start(self, attempt: PostRebaseRevalidation) -> PostRebaseRevalidation:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} "
                "WHERE task_id = ? AND result IN ('PENDING', 'WAITING_CI') LIMIT 1",
                (attempt.task_id,),
            ).fetchone()
            if active is not None:
                existing = _row(active)
                if _identity(existing) != _identity(attempt):
                    raise ValueError("another post-rebase revalidation is active")
                return existing
            conn.execute(
                f"INSERT INTO {POST_REBASE_REVALIDATIONS_TABLE} "
                "(attempt_id, task_id, source_run_id, repository_slug, head_branch, "
                "pull_request_number, previous_head, new_head, validated_tree, gates, "
                "security_review, ci_sha, ci_checks, result, failure_reason, started_at, "
                "updated_at, finished_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    attempt.attempt_id,
                    attempt.task_id,
                    attempt.source_run_id,
                    attempt.repository_slug,
                    attempt.head_branch,
                    attempt.pull_request_number,
                    attempt.previous_head,
                    attempt.new_head,
                    attempt.validated_tree,
                    _encode_gates(attempt.gates),
                    _encode_security(attempt.security_review),
                    attempt.ci_sha,
                    json.dumps(list(attempt.ci_checks), sort_keys=True),
                    attempt.result.value,
                    attempt.failure_reason,
                    encode_datetime(attempt.started_at),
                    encode_datetime(attempt.updated_at),
                    encode_datetime(attempt.finished_at) if attempt.finished_at else None,
                ),
            )
            row = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} WHERE attempt_id = ?",
                (attempt.attempt_id,),
            ).fetchone()
        assert row is not None
        return _row(row)

    def record_local_validation(
        self,
        attempt_id: str,
        validated_tree: str,
        gates: tuple[QualityGate, ...],
        security_review: SecurityReview,
    ) -> PostRebaseRevalidation:
        if not validated_tree.strip() or any(gate.is_blocking for gate in gates):
            raise ValueError("invalid local revalidation evidence")
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            current = _row(row)
            if current.result is RevalidationResult.WAITING_CI:
                if (
                    current.validated_tree != validated_tree
                    or current.gates != gates
                    or current.security_review != security_review
                ):
                    raise ValueError("revalidation local evidence mismatch")
                return current
            if current.result is not RevalidationResult.PENDING:
                raise ValueError("revalidation is not pending local validation")
            cursor = conn.execute(
                f"UPDATE {POST_REBASE_REVALIDATIONS_TABLE} SET validated_tree = ?, gates = ?, "
                "security_review = ?, result = 'WAITING_CI', updated_at = ? "
                "WHERE attempt_id = ? AND result = 'PENDING'",
                (
                    validated_tree,
                    _encode_gates(gates),
                    _encode_security(security_review),
                    encode_datetime(now),
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("revalidation local evidence race")
            row = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        assert row is not None
        return _row(row)

    def mark_passed(
        self, attempt_id: str, ci_sha: str, ci_checks: tuple[str, ...]
    ) -> PostRebaseRevalidation:
        if not ci_sha.strip():
            raise ValueError("exact CI SHA required")
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            current = _row(row)
            if current.result is RevalidationResult.PASSED:
                if current.ci_sha != ci_sha or current.ci_checks != ci_checks:
                    raise ValueError("revalidation CI evidence mismatch")
                return current
            if current.result is not RevalidationResult.WAITING_CI:
                raise ValueError("revalidation is not waiting for CI")
            cursor = conn.execute(
                f"UPDATE {POST_REBASE_REVALIDATIONS_TABLE} SET ci_sha = ?, ci_checks = ?, "
                "result = 'PASSED', updated_at = ?, finished_at = ? "
                "WHERE attempt_id = ? AND result = 'WAITING_CI'",
                (
                    ci_sha,
                    json.dumps(list(ci_checks), sort_keys=True),
                    encode_datetime(now),
                    encode_datetime(now),
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("revalidation pass race")
            row = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        assert row is not None
        return _row(row)

    def mark_failed(
        self,
        attempt_id: str,
        reason: str,
        *,
        validated_tree: str | None = None,
        gates: tuple[QualityGate, ...] = (),
        security_review: SecurityReview | None = None,
        ci_sha: str | None = None,
        ci_checks: tuple[str, ...] = (),
    ) -> PostRebaseRevalidation:
        clean = reason.strip()
        if not clean or len(clean) > 80:
            raise ValueError("sanitized revalidation failure reason required")
        now = datetime.now(UTC)
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(attempt_id)
            current = _row(row)
            if current.result is RevalidationResult.FAILED:
                if current.failure_reason != clean:
                    raise ValueError("revalidation failure evidence mismatch")
                return current
            if current.result is RevalidationResult.PASSED:
                raise ValueError("passed revalidation cannot fail")
            next_tree = validated_tree if validated_tree is not None else current.validated_tree
            next_gates = gates if gates else current.gates
            next_security = (
                security_review if security_review is not None else current.security_review
            )
            next_ci_sha = ci_sha if ci_sha is not None else current.ci_sha
            next_ci_checks = ci_checks if ci_checks else current.ci_checks
            cursor = conn.execute(
                f"UPDATE {POST_REBASE_REVALIDATIONS_TABLE} SET validated_tree = ?, gates = ?, "
                "security_review = ?, ci_sha = ?, ci_checks = ?, result = 'FAILED', "
                "failure_reason = ?, updated_at = ?, finished_at = ? "
                "WHERE attempt_id = ? AND result IN ('PENDING', 'WAITING_CI')",
                (
                    next_tree,
                    _encode_gates(next_gates),
                    _encode_security(next_security),
                    next_ci_sha,
                    json.dumps(list(next_ci_checks), sort_keys=True),
                    clean,
                    encode_datetime(now),
                    encode_datetime(now),
                    attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("revalidation failure race")
            row = conn.execute(
                f"SELECT * FROM {POST_REBASE_REVALIDATIONS_TABLE} WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
        assert row is not None
        return _row(row)


def _identity(value: PostRebaseRevalidation) -> tuple[object, ...]:
    return (
        value.task_id,
        value.source_run_id,
        value.repository_slug,
        value.head_branch,
        value.pull_request_number,
        value.previous_head,
        value.new_head,
    )


def _encode_gates(gates: tuple[QualityGate, ...]) -> str:
    return json.dumps(
        [
            {
                "name": gate.name,
                "status": gate.status.value,
                "detail": gate.detail,
                "required": gate.required,
            }
            for gate in gates
        ],
        sort_keys=True,
    )


def _decode_gates(raw: str) -> tuple[QualityGate, ...]:
    values = json.loads(raw)
    return tuple(
        QualityGate(
            name=str(item["name"]),
            status=QualityGateStatus(str(item["status"])),
            detail=item.get("detail"),
            required=bool(item["required"]),
        )
        for item in values
    )


def _encode_security(review: SecurityReview | None) -> str | None:
    if review is None:
        return None
    return json.dumps(
        {
            "rule_version": review.rule_version,
            "revision": review.revision,
            "findings": [
                {"rule_id": item.rule_id, "path_digest": item.path_digest, "line": item.line}
                for item in review.findings
            ],
        },
        sort_keys=True,
    )


def _decode_security(raw: str | None) -> SecurityReview | None:
    if raw is None:
        return None
    value = json.loads(raw)
    return SecurityReview(
        str(value["rule_version"]),
        str(value["revision"]),
        tuple(
            SecurityFinding(str(item["rule_id"]), str(item["path_digest"]), int(item["line"]))
            for item in value["findings"]
        ),
    )


def _row(row: sqlite3.Row) -> PostRebaseRevalidation:
    return PostRebaseRevalidation(
        task_id=str(row["task_id"]),
        source_run_id=str(row["source_run_id"]),
        repository_slug=str(row["repository_slug"]),
        head_branch=str(row["head_branch"]),
        pull_request_number=int(row["pull_request_number"]),
        previous_head=str(row["previous_head"]),
        new_head=str(row["new_head"]),
        attempt_id=str(row["attempt_id"]),
        validated_tree=row["validated_tree"],
        gates=_decode_gates(str(row["gates"])),
        security_review=_decode_security(row["security_review"]),
        ci_sha=row["ci_sha"],
        ci_checks=tuple(str(value) for value in json.loads(str(row["ci_checks"]))),
        result=RevalidationResult(str(row["result"])),
        failure_reason=row["failure_reason"],
        started_at=decode_datetime(str(row["started_at"])),
        updated_at=decode_datetime(str(row["updated_at"])),
        finished_at=(decode_datetime(str(row["finished_at"])) if row["finished_at"] else None),
    )


__all__ = ["SqlitePostRebaseRevalidationRepository"]
