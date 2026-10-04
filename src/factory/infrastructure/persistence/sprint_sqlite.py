"""SQLite sprint authorization with events in the existing E1 trace."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime

from factory.domain.backlog import WorkItem
from factory.domain.ports import SprintRepository
from factory.domain.sprint import SprintManifest, SprintState, SprintStep
from factory.infrastructure.persistence.schema import AUDIT_EVENTS_TABLE, SPRINTS_TABLE
from factory.infrastructure.persistence.sqlite_base import SqliteRepository


class SqliteSprintRepository(SqliteRepository, SprintRepository):
    """Keep authorization immutable and append state facts in one transaction."""

    def authorize(self, manifest: SprintManifest) -> None:
        encoded = json.dumps(asdict(manifest), sort_keys=True, separators=(",", ":"))
        with self._connect() as conn:
            existing = conn.execute(
                f"SELECT manifest FROM {SPRINTS_TABLE} WHERE sprint_id = ?", (manifest.sprint_id,)
            ).fetchone()
            if existing is not None:
                if existing["manifest"] != encoded:
                    raise ValueError("sprint authorization is immutable")
                latest = conn.execute(
                    f"SELECT sprint_id FROM {SPRINTS_TABLE} ORDER BY rowid DESC LIMIT 1"
                ).fetchone()
                if latest is None or latest["sprint_id"] != manifest.sprint_id:
                    raise ValueError("sprint is no longer current")
                return
            try:
                conn.execute(
                    f"INSERT INTO {SPRINTS_TABLE} VALUES (?, ?, 'ACTIVE', 0)",
                    (manifest.sprint_id, encoded),
                )
            except sqlite3.IntegrityError:
                raise ValueError("another sprint is active") from None
            self._event(conn, manifest.sprint_id, "SprintAuthorized", "authorize")

    def current(self) -> tuple[SprintManifest, SprintState, int] | None:
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {SPRINTS_TABLE} ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return None
        raw = json.loads(str(row["manifest"]))
        steps = tuple(
            SprintStep(WorkItem(**step["item"]), tuple(step["dependencies"]))
            for step in raw["steps"]
        )
        manifest = SprintManifest(
            str(raw["sprint_id"]),
            steps,
            int(raw["wip_limit"]),
            tuple(raw["stop_conditions"]),
            tuple(raw["pipeline"]),
        )
        return manifest, SprintState(row["state"]), int(row["position"])

    def find_for_work_item(self, project_id: str, provider: str, external_id: str) -> str | None:
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT sprint_id, state, manifest FROM {SPRINTS_TABLE}"
            ).fetchall()
        found: str | None = None
        for row in rows:
            # A cancelled sprint may be superseded by a re-authorized sprint
            # for the same external work item. It remains in the immutable
            # audit history, but must not create an active identity collision.
            if str(row["state"]) == SprintState.CANCELLED.value:
                continue
            manifest = json.loads(str(row["manifest"]))
            if any(
                step["item"].get("project_id", "ai-factory-lab") == project_id
                and step["item"]["provider"] == provider
                and step["item"]["external_id"] == external_id
                for step in manifest["steps"]
            ):
                if found is not None:
                    raise ValueError("WorkItem belongs to multiple Sprints")
                found = str(row["sprint_id"])
        return found

    def move(self, sprint_id: str, state: SprintState, position: int, event: str) -> None:
        if event not in {
            "WorkItemSelected",
            "SprintPaused",
            "SprintResumed",
            "SprintCancelled",
            "SprintAdvanced",
            "SprintCompleted",
        }:
            raise ValueError("unsupported sprint event")
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT state, position FROM {SPRINTS_TABLE} WHERE sprint_id = ?", (sprint_id,)
            ).fetchone()
            if row is None:
                raise KeyError(sprint_id)
            old_state = SprintState(row["state"])
            old_position = int(row["position"])
            valid = {
                "WorkItemSelected": old_state is SprintState.ACTIVE
                and state is SprintState.ACTIVE
                and position == old_position,
                "SprintPaused": old_state is SprintState.ACTIVE
                and state is SprintState.PAUSED
                and position == old_position,
                "SprintResumed": old_state is SprintState.PAUSED
                and state is SprintState.ACTIVE
                and position == old_position,
                "SprintCancelled": old_state in {SprintState.ACTIVE, SprintState.PAUSED}
                and state is SprintState.CANCELLED
                and position == old_position,
                "SprintAdvanced": old_state is SprintState.ACTIVE
                and state is SprintState.ACTIVE
                and position == old_position + 1,
                "SprintCompleted": old_state is SprintState.ACTIVE
                and state is SprintState.COMPLETE
                and position == old_position + 1,
            }
            if not valid[event]:
                raise ValueError("invalid sprint transition")
            key = f"{event}:{position}"
            if event in {"SprintPaused", "SprintResumed"}:
                count = conn.execute(
                    f"SELECT count(*) FROM {AUDIT_EVENTS_TABLE} WHERE aggregate_type = 'sprint' "
                    "AND aggregate_id = ? AND name = ?",
                    (sprint_id, event),
                ).fetchone()[0]
                key += f":{count + 1}"
            conn.execute(
                f"UPDATE {SPRINTS_TABLE} SET state = ?, position = ? WHERE sprint_id = ?",
                (state.value, position, sprint_id),
            )
            self._event(conn, sprint_id, event, key)

    @staticmethod
    def _event(conn: sqlite3.Connection, sprint_id: str, name: str, key: str) -> None:
        trace = f"sprint:{sprint_id}"
        conn.execute(
            f"INSERT OR IGNORE INTO {AUDIT_EVENTS_TABLE} "
            "(event_key, correlation_id, event_seq, name, task_id, causation_id, "
            "aggregate_type, aggregate_id, aggregate_version, occurred_at) "
            f"VALUES (?, ?, (SELECT count(*) + 1 FROM {AUDIT_EVENTS_TABLE} "
            "WHERE correlation_id = ?), ?, ?, ?, 'sprint', ?, "
            f"(SELECT count(*) + 1 FROM {AUDIT_EVENTS_TABLE} "
            "WHERE aggregate_type = 'sprint' AND aggregate_id = ?), ?)",
            (
                f"sprint:{sprint_id}:{key}",
                trace,
                trace,
                name,
                trace,
                key,
                sprint_id,
                sprint_id,
                datetime.now(UTC).isoformat(),
            ),
        )
