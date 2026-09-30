"""Security review facts in the existing E1 event stream."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from factory.domain.models import AgentRun, FactoryTask
from factory.domain.ports import SecurityInspector, SecurityReviewGate
from factory.domain.security import SECURITY_RULE_VERSION, SecurityReview
from factory.infrastructure.persistence.schema import AUDIT_EVENTS_TABLE, TASKS_TABLE
from factory.infrastructure.persistence.sqlite_base import SqliteRepository


class SqliteSecurityReviewGate(SqliteRepository, SecurityReviewGate):
    """Persist safe review findings and explicit decisions on the task trace."""

    def __init__(self, path: str, inspector: SecurityInspector) -> None:
        super().__init__(path)
        self._inspector = inspector

    def review(self, task: FactoryTask, run: AgentRun) -> SecurityReview:
        try:
            review = self._inspector.inspect(task, run)
        except Exception:
            unavailable = SecurityReview(
                SECURITY_RULE_VERSION, run.validated_revision or "unbound", ()
            )
            self._append(
                task,
                run,
                unavailable,
                "SecurityReviewUnavailable",
                {"rule_version": SECURITY_RULE_VERSION, "revision": unavailable.revision},
            )
            raise
        evidence: dict[str, object] = {
            "rule_version": review.rule_version,
            "revision": review.revision,
            "project_id": task.project_id,
            "critical": review.critical,
            "findings": [
                {"rule_id": item.rule_id, "path_digest": item.path_digest, "line": item.line}
                for item in review.findings
            ],
        }
        self._append(
            task,
            run,
            review,
            "SecurityReviewBlocked" if review.critical else "SecurityReviewPassed",
            evidence,
        )
        return review

    def is_overridden(self, task: FactoryTask, run: AgentRun, review: SecurityReview) -> bool:
        key = self._key("SecurityReviewOverridden", run, review)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM {AUDIT_EVENTS_TABLE} "
                "WHERE event_key = ? AND task_id = ? AND run_id = ?",
                (key, task.task_id, run.run_id),
            ).fetchone()
        return row is not None

    def record_override(
        self, task: FactoryTask, run: AgentRun, review: SecurityReview, *, actor: str, reason: str
    ) -> None:
        """Record an operator's explicit decision for one exact critical review."""
        if not review.critical or not actor.strip() or not reason.strip():
            raise ValueError("critical review, actor and reason required")
        if not self._recorded(task, run, review):
            raise ValueError("review evidence not recorded")
        evidence: dict[str, object] = {
            "rule_version": review.rule_version,
            "revision": review.revision,
            "decision": "override",
            "actor_digest": hashlib.sha256(actor.encode()).hexdigest()[:16],
            "reason_digest": hashlib.sha256(reason.encode()).hexdigest()[:16],
        }
        self._append(task, run, review, "SecurityReviewOverridden", evidence)

    def _recorded(self, task: FactoryTask, run: AgentRun, review: SecurityReview) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM {AUDIT_EVENTS_TABLE} "
                "WHERE event_key = ? AND task_id = ? AND run_id = ?",
                (self._key("SecurityReviewBlocked", run, review), task.task_id, run.run_id),
            ).fetchone()
        return row is not None

    @staticmethod
    def _key(name: str, run: AgentRun, review: SecurityReview) -> str:
        return f"security:{name}:{run.run_id}:{review.rule_version}:{review.revision}"

    def _append(
        self,
        task: FactoryTask,
        run: AgentRun,
        review: SecurityReview,
        name: str,
        evidence: dict[str, object],
    ) -> None:
        workspace = run.workspace
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT source_provider, source_repository, source_issue_number "
                f"FROM {TASKS_TABLE} "
                "WHERE task_id = ? AND project_id = ? AND target_repository = ?",
                (task.task_id, task.project_id, task.target_repository),
            ).fetchone()
            if row is None:
                raise ValueError("security review task identity mismatch")
            if conn.execute(
                f"SELECT 1 FROM {AUDIT_EVENTS_TABLE} WHERE event_key = ?",
                (self._key(name, run, review),),
            ).fetchone():
                return
            sequence = conn.execute(
                f"SELECT count(*) + 1 FROM {AUDIT_EVENTS_TABLE} WHERE correlation_id = ?",
                (task.task_id,),
            ).fetchone()[0]
            version = conn.execute(
                f"SELECT count(*) + 1 FROM {AUDIT_EVENTS_TABLE} "
                "WHERE aggregate_type = 'run' AND aggregate_id = ?",
                (run.run_id,),
            ).fetchone()[0]
            conn.execute(
                f"INSERT INTO {AUDIT_EVENTS_TABLE} "
                "(event_key, correlation_id, event_seq, name, task_id, run_id, workspace_id, "
                "causation_id, source_provider, source_repository, source_issue_number, "
                "aggregate_type, aggregate_id, aggregate_version, occurred_at, evidence) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'run', ?, ?, ?, ?)",
                (
                    self._key(name, run, review),
                    task.task_id,
                    sequence,
                    name,
                    task.task_id,
                    run.run_id,
                    workspace.workspace_id if workspace else None,
                    run.run_id,
                    row[0],
                    row[1],
                    row[2],
                    run.run_id,
                    version,
                    datetime.now(UTC).isoformat(),
                    json.dumps(evidence, sort_keys=True),
                ),
            )
