"""Final E1 reconciliation event, committed once for an exact delivery identity."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from factory.domain.feedback import FeedbackIdentity
from factory.domain.ports import FeedbackEventRepository
from factory.infrastructure.persistence.schema import AUDIT_EVENTS_TABLE
from factory.infrastructure.persistence.sqlite_base import SqliteRepository


class SqliteFeedbackEventRepository(SqliteRepository, FeedbackEventRepository):
    def is_completed(self, task_id: str) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT 1 FROM {AUDIT_EVENTS_TABLE} WHERE event_key = ?",
                ("delivery:" + task_id,),
            ).fetchone()
        return row is not None

    def record_resolution(self, identity: FeedbackIdentity, reason: str) -> None:
        if reason not in {"completed", "not_planned", "duplicate"}:
            raise ValueError("invalid resolution reason")
        evidence = json.dumps(
            {
                "source": "github_issue",
                "reason": reason,
                "project_id": identity.project_id,
                "work_item_provider": identity.work_item_provider,
                "work_item_id": identity.work_item_id,
                "issue_number": identity.issue_number,
                "pull_request_number": identity.pull_request_number,
                "commit_sha": identity.commit_sha,
            },
            sort_keys=True,
        )
        with self._connect() as conn:
            row = conn.execute(
                "SELECT t.status FROM tasks t JOIN agent_runs r ON r.task_id=t.task_id "
                "JOIN workspaces w ON w.workspace_id=r.workspace_id "
                "JOIN pull_requests p ON p.task_id=t.task_id AND p.run_id=r.run_id "
                "WHERE t.task_id=? AND t.project_id=? AND t.target_repository=? "
                "AND t.source_repository=? AND t.source_issue_number=? "
                "AND t.source_provider='github' "
                "AND t.reconciliation_origin IN ('trello', 'github-direct') "
                "AND ((t.reconciliation_origin='github-direct' AND t.expected_work_item_id IS NULL "
                "AND ? IS NULL) OR (t.reconciliation_origin='trello' "
                "AND t.expected_work_item_id=? AND ?='trello')) "
                "AND r.run_id=? AND r.project_id=? AND w.workspace_id=? "
                "AND w.repository_slug=t.target_repository "
                "AND p.repository_slug=t.target_repository AND p.head_branch=w.branch "
                "AND p.number=? AND p.commit_sha=?",
                (
                    identity.task_id,
                    identity.project_id,
                    identity.repository_slug,
                    identity.repository_slug,
                    identity.issue_number,
                    identity.work_item_provider,
                    identity.work_item_id,
                    identity.work_item_provider,
                    identity.run_id,
                    identity.project_id,
                    identity.workspace_id,
                    identity.pull_request_number,
                    identity.commit_sha,
                ),
            ).fetchone()
            if row is None or row["status"] != "DONE":
                raise ValueError("resolution identity mismatch")
            conn.execute(
                "INSERT OR IGNORE INTO audit_events "
                "(event_key, correlation_id, event_seq, name, task_id, run_id, workspace_id, "
                "pull_request_id, causation_id, source_provider, source_repository, "
                "source_issue_number, aggregate_type, aggregate_id, aggregate_version, "
                "occurred_at, evidence) VALUES (?, ?, "
                "(SELECT count(*) + 1 FROM audit_events WHERE correlation_id=?), "
                "'DeliveryReconciled', ?, ?, ?, "
                "(SELECT pull_request_id FROM pull_requests WHERE task_id=? AND run_id=?), ?, "
                "'github', ?, ?, 'task', ?, "
                "(SELECT count(*) + 1 FROM audit_events WHERE aggregate_type='task' "
                "AND aggregate_id=?), "
                "?, ?)",
                (
                    "resolution:" + identity.task_id + ":" + reason,
                    identity.task_id,
                    identity.task_id,
                    identity.task_id,
                    identity.run_id,
                    identity.workspace_id,
                    identity.task_id,
                    identity.run_id,
                    identity.run_id,
                    identity.repository_slug,
                    identity.issue_number,
                    identity.task_id,
                    identity.task_id,
                    datetime.now(UTC).isoformat(),
                    evidence,
                ),
            )

    def record_completed(self, identity: FeedbackIdentity) -> None:
        evidence = json.dumps(
            {
                "project_id": identity.project_id,
                "sprint_id": identity.sprint_id,
                "work_item_provider": identity.work_item_provider,
                "work_item_id": identity.work_item_id,
                "issue_number": identity.issue_number,
                "commit_sha": identity.commit_sha,
                "pull_request_number": identity.pull_request_number,
            },
            sort_keys=True,
        )
        with self._connect() as conn:
            match = conn.execute(
                "SELECT 1 FROM tasks t JOIN agent_runs r ON r.task_id = t.task_id "
                "JOIN workspaces w ON w.workspace_id = r.workspace_id "
                "JOIN pull_requests p ON p.task_id = t.task_id "
                "AND p.repository_slug = t.target_repository "
                "AND p.head_branch = w.branch AND p.run_id = r.run_id "
                "WHERE t.task_id = ? AND t.project_id = ? AND t.target_repository = ? "
                "AND t.source_provider = 'github' AND t.source_repository = ? "
                "AND t.source_issue_number = ? AND t.status = 'DONE' "
                "AND r.run_id = ? AND r.project_id = ? AND w.workspace_id = ? "
                "AND w.repository_slug = ? AND p.number = ? AND p.commit_sha = ? "
                "AND p.head_branch = ? "
                "AND p.merged = 1 AND ((t.reconciliation_origin = 'trello' "
                "AND t.expected_work_item_id = ? AND ? = 'trello' "
                "AND EXISTS (SELECT 1 FROM backlog_links b WHERE b.project_id = ? "
                "AND b.repository_slug = t.source_repository "
                "AND b.issue_number = t.source_issue_number AND b.provider = 'trello' "
                "AND b.external_id = t.expected_work_item_id AND b.state = 'MATERIALIZED')) "
                "OR (t.reconciliation_origin = 'github-direct' "
                "AND t.expected_work_item_id IS NULL AND ? IS NULL AND ? IS NULL "
                "AND NOT EXISTS (SELECT 1 FROM backlog_links b "
                "WHERE b.repository_slug = t.source_repository "
                "AND b.issue_number = t.source_issue_number)))",
                (
                    identity.task_id,
                    identity.project_id,
                    identity.repository_slug,
                    identity.repository_slug,
                    identity.issue_number,
                    identity.run_id,
                    identity.project_id,
                    identity.workspace_id,
                    identity.repository_slug,
                    identity.pull_request_number,
                    identity.commit_sha,
                    identity.branch,
                    identity.work_item_id,
                    identity.work_item_provider,
                    identity.project_id,
                    identity.work_item_provider,
                    identity.work_item_id,
                ),
            ).fetchone()
            if match is None:
                raise ValueError("feedback identity mismatch")
            conn.execute(
                f"INSERT OR IGNORE INTO {AUDIT_EVENTS_TABLE} "
                "(event_key, correlation_id, event_seq, name, task_id, run_id, "
                "workspace_id, pull_request_id, causation_id, source_provider, "
                "source_repository, source_issue_number, aggregate_type, aggregate_id, "
                "aggregate_version, occurred_at, evidence) "
                f"SELECT ?, t.task_id, (SELECT count(*) + 1 FROM {AUDIT_EVENTS_TABLE} "
                "WHERE correlation_id = t.task_id), 'DeliveryReconciled', t.task_id, ?, ?, "
                "p.pull_request_id, ?, t.source_provider, t.source_repository, "
                "t.source_issue_number, 'task', t.task_id, "
                f"(SELECT count(*) + 1 FROM {AUDIT_EVENTS_TABLE} "
                "WHERE aggregate_type = 'task' AND aggregate_id = t.task_id), ?, ? "
                "FROM tasks t JOIN pull_requests p ON p.task_id = t.task_id "
                "AND p.number = ? AND p.repository_slug = ? WHERE t.task_id = ?",
                (
                    "delivery:" + identity.task_id,
                    identity.run_id,
                    identity.workspace_id,
                    identity.run_id,
                    datetime.now(UTC).isoformat(),
                    evidence,
                    identity.pull_request_number,
                    identity.repository_slug,
                    identity.task_id,
                ),
            )
