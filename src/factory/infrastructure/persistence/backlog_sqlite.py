"""Durable, immutable backlog reservation and issue link."""

from __future__ import annotations

import sqlite3

from factory.domain.backlog import MaterializedIssue, WorkItem
from factory.domain.ports import BacklogLinkRepository
from factory.infrastructure.persistence.schema import BACKLOG_LINKS_TABLE
from factory.infrastructure.persistence.sqlite_base import SqliteRepository


class SqliteBacklogLinkRepository(SqliteRepository, BacklogLinkRepository):
    """SQLite compare-and-swap boundary for one remote write per work item."""

    def reserve(self, item: WorkItem) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"INSERT OR IGNORE INTO {BACKLOG_LINKS_TABLE} "
                "(provider, external_id, repository_slug, state) VALUES (?, ?, ?, 'RESERVED')",
                (item.provider, item.external_id, item.target_repository),
            )
            row = conn.execute(
                f"SELECT repository_slug FROM {BACKLOG_LINKS_TABLE} "
                "WHERE provider = ? AND external_id = ?",
                (item.provider, item.external_id),
            ).fetchone()
            if row is None or row["repository_slug"] != item.target_repository:
                raise ValueError("backlog link repository identity changed")
            return cursor.rowcount == 1

    def begin_write(self, item: WorkItem) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {BACKLOG_LINKS_TABLE} SET state = 'POSTING' "
                "WHERE provider = ? AND external_id = ? AND repository_slug = ? "
                "AND state = 'RESERVED'",
                (item.provider, item.external_id, item.target_repository),
            )
            return cursor.rowcount == 1

    def get_issue(self, provider: str, external_id: str) -> MaterializedIssue | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {BACKLOG_LINKS_TABLE} WHERE provider = ? AND external_id = ?",
                (provider, external_id),
            ).fetchone()
        if row is None or row["state"] != "MATERIALIZED":
            return None
        return MaterializedIssue(row["repository_slug"], row["issue_number"], row["issue_url"])

    def complete(self, item: WorkItem, issue: MaterializedIssue) -> None:
        if issue.repository_slug != item.target_repository or issue.number <= 0:
            raise ValueError("issue identity does not match backlog reservation")
        try:
            with self._connect() as conn:
                cursor = conn.execute(
                    f"UPDATE {BACKLOG_LINKS_TABLE} "
                    "SET state = 'MATERIALIZED', issue_number = ?, issue_url = ? "
                    "WHERE provider = ? AND external_id = ? AND repository_slug = ? "
                    "AND state != 'MATERIALIZED'",
                    (
                        issue.number,
                        issue.url,
                        item.provider,
                        item.external_id,
                        item.target_repository,
                    ),
                )
                if cursor.rowcount == 0:
                    row = conn.execute(
                        f"SELECT issue_number, issue_url FROM {BACKLOG_LINKS_TABLE} "
                        "WHERE provider = ? AND external_id = ?",
                        (item.provider, item.external_id),
                    ).fetchone()
                    if (
                        row is None
                        or row["issue_number"] != issue.number
                        or row["issue_url"] != issue.url
                    ):
                        raise ValueError("conflicting backlog issue link")
        except sqlite3.IntegrityError:
            raise ValueError("issue is linked to another work item") from None
