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

    def __init__(self, path: str, *, read_only: bool = False) -> None:
        super().__init__(path, read_only=read_only)

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
                        head_branch, base_branch, title, number, url, merged, opened_at,
                        commit_sha
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                        pull_request.commit_sha,
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

    def record_revision(
        self, pull_request: PullRequest, run_id: str, commit_sha: str
    ) -> PullRequest:
        if not commit_sha.strip() or not run_id.strip() or pull_request.run_id is None:
            raise ValueError("invalid published revision")
        with self._connect() as conn:
            source = conn.execute(
                f"""
                SELECT run.task_id, run.project_id, run.workspace_id,
                       task.project_id AS task_project_id,
                       task.target_repository,
                       workspace.repository_slug AS workspace_repository,
                       workspace.branch
                  FROM {PULL_REQUESTS_TABLE} AS pr
                  JOIN agent_runs AS run ON run.run_id = pr.run_id
                  JOIN tasks AS task ON task.task_id = run.task_id
                  JOIN workspaces AS workspace ON workspace.workspace_id = run.workspace_id
                 WHERE pr.run_id = ? AND pr.task_id = ?
                """,
                (pull_request.run_id, pull_request.task_id),
            ).fetchone()
            destination = conn.execute(
                """
                SELECT run.task_id, run.project_id, run.workspace_id,
                       task.project_id AS task_project_id,
                       task.target_repository,
                       workspace.repository_slug AS workspace_repository,
                       workspace.branch
                  FROM agent_runs AS run
                  JOIN tasks AS task ON task.task_id = run.task_id
                  JOIN workspaces AS workspace ON workspace.workspace_id = run.workspace_id
                 WHERE run.run_id = ?
                """,
                (run_id,),
            ).fetchone()
            if (
                source is None
                or destination is None
                or tuple(source) != tuple(destination)
                or destination["task_id"] != pull_request.task_id
                or source["project_id"] != source["task_project_id"]
                or source["workspace_repository"] != source["target_repository"]
                or destination["project_id"] != destination["task_project_id"]
                or destination["workspace_repository"] != destination["target_repository"]
                or destination["target_repository"] != pull_request.repository_slug
                or destination["branch"] != pull_request.head_branch
            ):
                raise ValueError("pull request revision identity mismatch")
            cursor = conn.execute(
                f"UPDATE {PULL_REQUESTS_TABLE} SET run_id = ?, commit_sha = ?, merged = 0 "
                "WHERE run_id = ? AND task_id = ? AND repository_slug = ? "
                "AND head_branch = ? AND base_branch = ? AND number = ?",
                (
                    run_id,
                    commit_sha,
                    pull_request.run_id,
                    pull_request.task_id,
                    pull_request.repository_slug,
                    pull_request.head_branch,
                    pull_request.base_branch,
                    pull_request.number,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("pull request revision identity mismatch")
        result = self.get_for_run(run_id)
        assert result is not None
        return result

    def record_provider_revision(self, pull_request: PullRequest, commit_sha: str) -> PullRequest:
        if (
            not commit_sha.strip()
            or pull_request.run_id is None
            or pull_request.task_id is None
            or pull_request.number is None
        ):
            raise ValueError("invalid provider revision identity")
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {PULL_REQUESTS_TABLE} SET commit_sha = ?, merged = 0 "
                "WHERE run_id = ? AND task_id = ? AND repository_slug = ? "
                "AND head_branch = ? AND base_branch = ? AND number = ?",
                (
                    commit_sha,
                    pull_request.run_id,
                    pull_request.task_id,
                    pull_request.repository_slug,
                    pull_request.head_branch,
                    pull_request.base_branch,
                    pull_request.number,
                ),
            )
            if cursor.rowcount not in {0, 1}:
                raise ValueError("provider revision identity mismatch")
            row = conn.execute(
                f"SELECT * FROM {PULL_REQUESTS_TABLE} WHERE run_id = ?",
                (pull_request.run_id,),
            ).fetchone()
        if row is None:
            raise ValueError("provider revision identity mismatch")
        return _row_to_pull_request(row)

    def record_merged(self, pull_request: PullRequest) -> PullRequest:
        if (
            pull_request.run_id is None
            or pull_request.task_id is None
            or pull_request.number is None
            or not pull_request.commit_sha
        ):
            raise ValueError("invalid pull request merge identity")
        identity = (
            pull_request.run_id,
            pull_request.task_id,
            pull_request.repository_slug,
            pull_request.head_branch,
            pull_request.base_branch,
            pull_request.number,
            pull_request.commit_sha,
        )
        with self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE {PULL_REQUESTS_TABLE} SET merged = 1 "
                "WHERE run_id = ? AND task_id = ? AND repository_slug = ? "
                "AND head_branch = ? AND base_branch = ? AND number = ? "
                "AND commit_sha = ? AND merged = 0",
                identity,
            )
            if cursor.rowcount == 0:
                row = conn.execute(
                    f"SELECT * FROM {PULL_REQUESTS_TABLE} WHERE run_id = ?",
                    (pull_request.run_id,),
                ).fetchone()
                if (
                    row is None
                    or (
                        row["run_id"],
                        row["task_id"],
                        row["repository_slug"],
                        row["head_branch"],
                        row["base_branch"],
                        row["number"],
                        row["commit_sha"],
                    )
                    != identity
                    or not row["merged"]
                ):
                    raise ValueError("pull request merge identity mismatch")
            row = conn.execute(
                f"SELECT * FROM {PULL_REQUESTS_TABLE} WHERE run_id = ?",
                (pull_request.run_id,),
            ).fetchone()
        assert row is not None
        return _row_to_pull_request(row)

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
        commit_sha=row["commit_sha"],
    )


__all__ = ["SqlitePullRequestRepository"]
