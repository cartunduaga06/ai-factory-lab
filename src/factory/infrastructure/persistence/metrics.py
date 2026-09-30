"""Deterministic sprint metrics from task, run and transition rows."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime

from factory.infrastructure.persistence.sqlite_base import SqliteRepository


@dataclass(frozen=True, slots=True)
class SprintMetrics:
    project_id: str
    tasks: int
    throughput: int
    lead_time_seconds: float | None
    success_rate: float | None
    first_pass_gates_rate: float | None
    retry_rate: float | None
    human_interventions: int

    def as_dict(self) -> dict[str, str | int | float | None]:
        return asdict(self)


class SqliteSprintMetrics(SqliteRepository):
    """Read facts only; never infer outcomes from titles, bodies or comments."""

    def report(self) -> tuple[SprintMetrics, ...]:
        with self._connect() as conn:
            tasks = conn.execute(
                "SELECT task_id, project_id, status, created_at FROM tasks ORDER BY task_id"
            ).fetchall()
            runs = conn.execute(
                "SELECT task_id, status, gates FROM agent_runs ORDER BY created_at, rowid"
            ).fetchall()
            transitions = conn.execute(
                "SELECT task_id, to_status, occurred_at FROM transitions "
                "ORDER BY occurred_at, rowid"
            ).fetchall()
        by_run: dict[str, list[tuple[str, str]]] = {}
        by_transition: dict[str, list[tuple[str, str]]] = {}
        for row in runs:
            by_run.setdefault(str(row["task_id"]), []).append(
                (str(row["status"]), str(row["gates"]))
            )
        for row in transitions:
            by_transition.setdefault(str(row["task_id"]), []).append(
                (str(row["to_status"]), str(row["occurred_at"]))
            )
        groups: dict[str, list[object]] = {"global": list(tasks)}
        for task in tasks:
            groups.setdefault(str(task["project_id"]), []).append(task)
        return tuple(
            self._calculate(project, rows, by_run, by_transition)
            for project, rows in sorted(groups.items(), key=lambda pair: pair[0] != "global")
        )

    @staticmethod
    def _calculate(
        project: str,
        tasks: list[object],
        runs: dict[str, list[tuple[str, str]]],
        transitions: dict[str, list[tuple[str, str]]],
    ) -> SprintMetrics:
        from sqlite3 import Row

        done = terminal = started = retries = first_pass = interventions = 0
        lead_times: list[float] = []
        for value in tasks:
            assert isinstance(value, Row)
            task_id = str(value["task_id"])
            task_runs = runs.get(task_id, [])
            history = transitions.get(task_id, [])
            if value["status"] == "DONE":
                done += 1
                completion = next((at for phase, at in history if phase == "DONE"), None)
                if completion is not None:
                    lead_times.append(
                        (
                            datetime.fromisoformat(completion)
                            - datetime.fromisoformat(value["created_at"])
                        ).total_seconds()
                    )
            if value["status"] in {"DONE", "FAILED", "CANCELLED"}:
                terminal += 1
            if task_runs:
                started += 1
                retries += len(task_runs) > 1
                status, encoded = task_runs[0]
                gates = json.loads(encoded)
                if (
                    status == "SUCCEEDED"
                    and isinstance(gates, list)
                    and gates
                    and all(
                        isinstance(gate, dict)
                        and (not gate.get("required", True) or gate.get("status") == "PASSED")
                        for gate in gates
                    )
                ):
                    first_pass += 1
            interventions += sum(
                phase in {"WAITING_HUMAN", "CHANGES_REQUESTED"} for phase, _ in history
            )
        return SprintMetrics(
            project_id=project,
            tasks=len(tasks),
            throughput=done,
            lead_time_seconds=round(sum(lead_times) / len(lead_times), 2) if lead_times else None,
            success_rate=round(done / terminal, 4) if terminal else None,
            first_pass_gates_rate=round(first_pass / started, 4) if started else None,
            retry_rate=round(retries / started, 4) if started else None,
            human_interventions=interventions,
        )
