"""SQLite implementation of :class:`~factory.domain.ports.PullRequestRepository`.

A pull request created for a run is durable: it must survive a process restart so
publication is idempotent and a crash after the provider created the PR but before
it was recorded can be reconciled. Standard-library ``sqlite3`` only.

``save`` is a plain insert, never an upsert. The schema carries two unique
constraints — one PR per run, and one active publication identity per
``(repository_slug, head_branch)`` — so a duplicate is refused by storage rather
than overwriting the first row. That is the defense-in-depth guard behind
one-PR-per-run publication idempotency: even if an application-level check is
bypassed, the database rejects the duplicate.

The raw SQLite error is never surfaced: its text names table columns and can echo
stored values, so only the sanitized :class:`DuplicatePullRequestError` crosses
the boundary.
"""

from __future__ import annotations

import sqlite3
import uuid

from factory.domain.errors import DuplicatePullRequestError, PersistenceError
from factory.domain.models import PullRequest
from factory.domain.ports import PullRequestRepository
from factory.infrastructure.persistence.codec import decode_datetime, encode_datetime
from factory.infrastructure.persistence.schema import PULL_REQUESTS_TABLE
from factory.infrastructure.persistence.sqlite_base import SqliteRepository


class SqlitePullRequestRepository(SqliteRepository, PullRequestRepository):
    """Durable pull-request storage backed by a SQLite file."""

    def __repr__(self) -> str:
        return f"SqlitePullRequestRepository(path={self._path!r})"

    # -- PullRequestRepository ---------------------------------------------

    def save(self, pull_request: PullRequest) -> PullRequest:
        error: Exception | None = None
        try:
            with self._connect() as conn:
                conn.execute(
                    f"""
                    INSERT INTO {PULL_REQUESTS_TABLE} (
                        pull_request_id, run_id, task_id, repository_slug,
                        head_branch, base_branch, title, number, url, merged, opened_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid.uuid4()),
                        pull_request.run_id,
                        pull_request.task_id,
                        pull_request.repository_slug,
                        pull_request.head_branch,
                        pull_request.base_branch,
                        pull_request.title,
                        pull_request.number,
                        pull_request.url,
                        1 if pull_request.merged else 0,
                        encode_datetime(pull_request.opened_at),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            error = self._duplicate_error(exc, pull_request)
        if error is not None:
            # Raised outside the ``except`` block so the raw SQLite error is not
            # retained as ``__context__``. Its text names columns and can echo
            # stored values, so it must not be reachable from the sanitized error.
            raise error
        return pull_request

    def get_for_run(self, run_id: str) -> PullRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {PULL_REQUESTS_TABLE} WHERE run_id = ?", (run_id,)
            ).fetchone()
        return _row_to_pull_request(row) if row is not None else None

    def find_by_branch(self, repository_slug: str, head_branch: str) -> PullRequest | None:
        with self._connect() as conn:
            row = conn.execute(
                f"""
                SELECT * FROM {PULL_REQUESTS_TABLE}
                 WHERE repository_slug = ? AND head_branch = ?
                """,
                (repository_slug, head_branch),
            ).fetchone()
        return _row_to_pull_request(row) if row is not None else None

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _duplicate_error(exc: sqlite3.IntegrityError, pull_request: PullRequest) -> Exception:
        """Map a SQLite integrity failure to a sanitized factory error.

        The raw SQLite text is discarded: it names columns and can include stored
        values. Only the fact that a PR already exists for this run/branch crosses
        the boundary. ``raise ... from None`` at the call site keeps it out of
        ``__cause__``/``__context__``.
        """
        if "UNIQUE" not in str(exc).upper():
            return PersistenceError("pull request could not be persisted")
        run_id = pull_request.run_id
        return DuplicatePullRequestError(
            run_id or "",
            pull_request.repository_slug,
            pull_request.head_branch,
        )


def _row_to_pull_request(row: sqlite3.Row) -> PullRequest:
    return PullRequest(
        repository_slug=row["repository_slug"],
        head_branch=row["head_branch"],
        base_branch=row["base_branch"],
        title=row["title"],
        number=row["number"],
        url=row["url"],
        task_id=row["task_id"],
        run_id=row["run_id"],
        opened_at=decode_datetime(row["opened_at"]),
        merged=bool(row["merged"]),
    )


__all__ = ["SqlitePullRequestRepository"]
