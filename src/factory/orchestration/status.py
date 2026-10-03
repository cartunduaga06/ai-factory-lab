"""Read-only, secret-safe operator projection of durable factory state."""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

from factory.domain.enums import RunStatus, TaskKind, TaskStatus, ValidationOutcome
from factory.domain.models import AgentRun, FactoryTask, StatusSnapshot
from factory.domain.ports import PullRequestRepository, RunRepository, TaskRepository


class StatusService:
    """Project persisted state without trusting agent or provider prose."""

    def __init__(
        self,
        tasks: TaskRepository,
        runs: RunRepository,
        pull_requests: PullRequestRepository,
        *,
        heartbeat_interval: float = 180.0,
        missed_heartbeats: int = 2,
    ) -> None:
        if heartbeat_interval <= 0 or missed_heartbeats < 1:
            raise ValueError("heartbeat interval and missed threshold must be positive")
        self._tasks = tasks
        self._runs = runs
        self._pull_requests = pull_requests
        self._stale_after = timedelta(seconds=heartbeat_interval * missed_heartbeats)

    def current(self, *, now: datetime | None = None) -> StatusSnapshot:
        """Show active work first, then the most recently changed finished task."""
        tasks = self._tasks.list()
        if not tasks:
            return StatusSnapshot("IDLE")
        active_priority = {
            TaskStatus.RUNNING: 0,
            TaskStatus.VALIDATING: 1,
            TaskStatus.PR_OPEN: 2,
            TaskStatus.WAITING_HUMAN: 3,
            TaskStatus.CLAIMED: 4,
        }
        active = [
            task
            for task in tasks
            if task.status in active_priority
            or (task.status is TaskStatus.READY and self._latest_run_failed_gates(task.task_id))
        ]
        if active:
            selected = min(
                active,
                key=lambda task: (
                    active_priority.get(task.status, 4),
                    -task.updated_at.timestamp(),
                ),
            )
        else:
            selected = max(tasks, key=lambda task: task.updated_at)
            if selected.status in {TaskStatus.READY, TaskStatus.DISCOVERED}:
                return StatusSnapshot("IDLE")
        return self.for_task(selected.task_id, now=now)

    def for_task(self, task_id: str, *, now: datetime | None = None) -> StatusSnapshot:
        """Return one task's bounded history and latest run, or raise KeyError."""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        runs = self._runs.list_runs(task_id)
        run = runs[-1] if runs else None
        transitions = self._tasks.history(task_id)[-6:]
        last_transition = transitions[-1].occurred_at if transitions else None
        current_time = now or datetime.now(UTC)
        phase = task.status.value
        agent_liveness = None
        if run is not None and run.adapter.value == "CODEX" and not run.is_terminal:
            if run.agent_heartbeat is None:
                agent_liveness = "unknown"
            elif current_time - run.agent_heartbeat > self._stale_after:
                agent_liveness = "stalled"
            else:
                agent_liveness = "alive"
        elif run is not None and run.adapter.value == "CODEX":
            agent_liveness = "stopped"
        if task.status is TaskStatus.RUNNING and run is not None and not run.is_terminal:
            heartbeat = run.last_heartbeat or run.started_at
            if heartbeat is not None and current_time - heartbeat > self._stale_after:
                phase = "STALLED"
        pr_url = self._pr_url(run) if run is not None else None
        evidence = self._evidence(task, run, phase)
        blocked_reason = self._safe_blocked_reason(task.blocked_reason)
        action = self._action(phase, blocked_reason, self._latest_run_failed_gates(task.task_id))
        source = task.source
        issue_url = None
        if source is not None and source.provider == "github":
            issue_url = (
                f"https://github.com/{quote(source.repository_slug, safe='/')}/"
                f"issues/{source.issue_number}"
            )
        return StatusSnapshot(
            phase=phase,
            task_id=task.task_id,
            project_id=task.project_id,
            repository=task.target_repository,
            issue_url=issue_url,
            # Provider run IDs are opaque input and may contain echoed secrets.
            run_id=hashlib.sha256(run.run_id.encode()).hexdigest()[:12] if run else None,
            agent=run.adapter.value if run else None,
            workspace_id=run.workspace.workspace_id if run and run.workspace else None,
            branch=run.workspace.branch if run and run.workspace else None,
            started_at=self._iso(run.started_at) if run else None,
            last_heartbeat=self._iso(run.last_heartbeat) if run else None,
            agent_heartbeat=self._iso(run.agent_heartbeat) if run else None,
            agent_liveness=agent_liveness,
            last_transition=self._iso(last_transition),
            finished_at=self._iso(
                last_transition
                if task.status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
                and last_transition is not None
                else (run.finished_at if run else None)
            ),
            pr_url=pr_url,
            commit_sha=self._commit_sha(run),
            blocked_reason=blocked_reason,
            action=action,
            evidence=evidence,
            gates=self._gates(run),
            previous_gates=tuple(
                f"Run {hashlib.sha256(previous.run_id.encode()).hexdigest()[:12]}: {gate}"
                for previous in runs[-6:-1]
                for gate in self._gates(previous)
            ),
            history=tuple(
                f"{self._iso(item.occurred_at)} {item.to_status.value}" for item in transitions
            ),
        )

    def _latest_run_failed_gates(self, task_id: str) -> bool:
        runs = self._runs.list_runs(task_id)
        return bool(runs) and runs[-1].validation_outcome is ValidationOutcome.GATES_FAILED

    @staticmethod
    def _gates(run: AgentRun | None) -> tuple[str, ...]:
        """Expose only bounded gate labels, statuses and allowlisted process results."""
        if run is None:
            return ()
        result = []
        for gate in run.gates[:40]:
            name = gate.name if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", gate.name) else "check"
            detail = gate.detail or ""
            if not re.fullmatch(
                r"(?:exit_code=-?\d{1,3}|timeout|spawn_error|redacted|runner_error|runner_mismatch|"
                r"no_workspace|workspace repair unavailable|workspace identity mismatch|"
                r"workspace repair failed|workspace inspection failed|"
                r"workspace changed during validation)",
                detail,
            ):
                detail = ""
            suffix = f" ({detail})" if detail else ""
            requirement = "required" if gate.required else "optional"
            result.append(f"{name}: {gate.status.value} ({requirement}){suffix}")
        return tuple(result)

    def _pr_url(self, run: AgentRun) -> str | None:
        pr = self._pull_requests.get_for_run(run.run_id)
        if pr is None and run.workspace is not None:
            pr = self._pull_requests.find_by_branch(
                run.workspace.repository_slug, run.workspace.branch
            )
        if pr is None or pr.task_id != run.task_id or pr.number is None or pr.number <= 0:
            return None
        return f"https://github.com/{quote(pr.repository_slug, safe='/')}/pull/{pr.number}"

    def _commit_sha(self, run: AgentRun | None) -> str | None:
        if run is None:
            return None
        pr = self._pull_requests.get_for_run(run.run_id)
        if pr is None and run.workspace is not None:
            pr = self._pull_requests.find_by_branch(
                run.workspace.repository_slug, run.workspace.branch
            )
        if pr is None or pr.task_id != run.task_id:
            return None
        sha = pr.commit_sha
        return sha if sha is not None and re.fullmatch(r"[0-9a-fA-F]{40,64}", sha) else None

    @staticmethod
    def _safe_blocked_reason(reason: str | None) -> str | None:
        if reason == "security review blocked publication":
            return "Security review requires operator attention"
        if reason == "pull request closed without merge; human review required":
            return "Pull request closed without merge"
        if reason == "security review unavailable":
            return "Security review could not be completed"
        if reason is not None and re.fullmatch(r"[A-Za-z0-9 _.-]{1,80}", reason):
            return reason
        return "Blocked; inspect the task record" if reason else None

    @staticmethod
    def _action(phase: str, blocked_reason: str | None, failed_gates: bool) -> str | None:
        if phase == "WAITING_HUMAN":
            return "Review the pull request"
        if phase == "STALLED":
            return "Check worker liveness"
        if blocked_reason:
            return "Review the blocking reason"
        if phase == "READY" and failed_gates:
            return "QA rework queued"
        if phase == "FAILED":
            return "Review the failed run"
        return None

    @staticmethod
    def _evidence(task: FactoryTask, run: AgentRun | None, phase: str) -> str | None:
        if phase == "STALLED":
            return "No verified heartbeat within the configured threshold"
        if phase == "FAILED":
            if run is not None and run.status is RunStatus.FAILED:
                if run.summary == "action=scratch_artifact; dispatch_failed":
                    return "Operational agent dispatch failed"
                if run.summary is not None:
                    match = re.fullmatch(
                        r"action=scratch_artifact; exit_code=(-?\d{1,3}|None); "
                        r"stdout_bytes=\d{1,7}; stderr_bytes=\d{1,7}",
                        run.summary,
                    )
                    if match is not None:
                        return f"Operational agent exit code: {match.group(1)}"
                return "Agent reported failure"
            return "Task failed"
        if phase in {"BLOCKED", "VALIDATING", "READY"} and run is not None:
            failed = sum(gate.is_blocking for gate in run.gates)
            if failed:
                return f"{failed} required check(s) did not pass"
        if phase == "DONE":
            return "Human-confirmed PR merge" if task.kind is TaskKind.CODE else "Accepted outcome"
        return None

    @staticmethod
    def _iso(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None
