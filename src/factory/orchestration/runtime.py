"""One-shot, resumable runtime for the already implemented factory phases."""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass

from factory.domain.enums import AgentKind, RunStatus, TaskKind, TaskStatus, ValidationOutcome
from factory.domain.models import AgentAdapter, AgentRun, FactoryTask, QualityGateSpec, Repository
from factory.domain.operational import (
    OperationalCapability,
    OperationalPolicy,
    parse_scratch_artifact,
)
from factory.domain.ports import (
    OperationalAcceptance,
    PullRequestRepository,
    PullRequestSink,
    PullRequestState,
    PullRequestStateSource,
    QualityGateRunner,
    RunRepository,
    TaskRepository,
    WorkspaceProvisioner,
    WorkspacePublisher,
    WorkspaceRevisionInspector,
)
from factory.orchestration.dispatch import DispatchService
from factory.orchestration.intake import IntakeSummary, IssueIntakeService
from factory.orchestration.publication import PublicationResult, PublicationService
from factory.orchestration.sprint import SprintService
from factory.orchestration.tracking import RunRefresh, RunTrackingService


@dataclass(slots=True, frozen=True)
class RuntimeResult:
    """Sanitized result of one bounded invocation."""

    task_id: str | None
    run_id: str | None
    task_status: TaskStatus | None
    branch: str | None
    validation: ValidationOutcome | None
    pull_request_number: int | None
    pull_request_url: str | None
    outcome: str
    intake: IntakeSummary


class FactoryRuntime:
    """Drive at most one task through intake, execution and mode-specific acceptance."""

    def __init__(
        self,
        *,
        intake: IssueIntakeService,
        intake_repository: Repository,
        tasks: TaskRepository,
        runs: RunRepository,
        adapter: AgentAdapter,
        provisioner: WorkspaceProvisioner,
        workspace_root: str,
        gate_specs: tuple[QualityGateSpec, ...],
        task_gate_specs: Mapping[str, Sequence[QualityGateSpec]] | None = None,
        gate_runner: QualityGateRunner,
        revision_inspector: WorkspaceRevisionInspector,
        publisher: WorkspacePublisher,
        pull_request_sink: PullRequestSink,
        pull_requests: PullRequestRepository,
        base_branch: str,
        pull_request_state: PullRequestStateSource | None = None,
        operational_policy: OperationalPolicy | None = None,
        operational_provisioner: WorkspaceProvisioner | None = None,
        operational_root: str | None = None,
        operational_acceptance: OperationalAcceptance | None = None,
        code_capable: bool = True,
        poll_interval: float = 5.0,
        timeout: float = 1800.0,
        heartbeat_interval: float = 180.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        status_pulse: Callable[[], None] | None = None,
        backlog_reconcile: Callable[[], object] | None = None,
        sprint: SprintService | None = None,
    ) -> None:
        self._intake = intake
        self._intake_repository = intake_repository
        self._tasks = tasks
        self._runs = runs
        self._adapter = adapter
        self._dispatch = DispatchService(
            tasks,
            runs,
            provisioner=provisioner,
            workspace_root=workspace_root,
        )
        self._operational_dispatch = (
            DispatchService(
                tasks,
                runs,
                provisioner=operational_provisioner,
                workspace_root=operational_root,
            )
            if operational_provisioner is not None and operational_root is not None
            else None
        )
        self._code_capable = code_capable
        self._tracking = RunTrackingService(
            tasks,
            runs,
            gate_specs=gate_specs,
            task_gate_specs=task_gate_specs,
            gate_runner=gate_runner,
            revision_inspector=revision_inspector,
            provisioner=provisioner,
            pull_requests=pull_requests,
            pull_request_sink=pull_request_sink,
            base_branch=base_branch,
            operational_acceptance=operational_acceptance,
            heartbeat_interval=heartbeat_interval,
        )
        self._publication = PublicationService(
            tasks,
            runs,
            pull_requests,
            publisher=publisher,
            sink=pull_request_sink,
            base_branch=base_branch,
            default_branch=base_branch,
        )
        self._pull_requests = pull_requests
        self._pull_request_state = pull_request_state
        self._operational_policy = operational_policy or OperationalPolicy()
        self._poll_interval = max(0.0, poll_interval)
        self._timeout = max(0.0, timeout)
        self._sleep = sleep
        self._monotonic = monotonic
        self._status_pulse = status_pulse
        self._backlog_reconcile = backlog_reconcile
        self._sprint = sprint

    def run_once(self) -> RuntimeResult:
        """Run intake and reconcile exactly one task, never merging or deploying."""
        try:
            if self._sprint is not None and self._sprint.is_paused():
                self._reconcile_human_reviews()
                return RuntimeResult(
                    None, None, None, None, None, None, None, "SPRINT_PAUSED", IntakeSummary()
                )
            if self._sprint is not None and not self._sprint.prepare():
                outcome = "SPRINT_PAUSED" if self._sprint.is_paused() else "NO_ELIGIBLE_TASK"
                return RuntimeResult(
                    None, None, None, None, None, None, None, outcome, IntakeSummary()
                )
            result = self._run_once()
            if self._sprint is not None and result.task_id is not None:
                self._sprint.observe(self._tasks.get(result.task_id))
            return result
        finally:
            self._pulse_status()

    def _run_once(self) -> RuntimeResult:
        """Drive the existing one-shot lifecycle."""
        if self._backlog_reconcile is not None and self._sprint is None:
            self._backlog_reconcile()
        intake = self._intake.intake(self._intake_repository)
        self._reconcile_human_reviews()
        task = self._select_task()
        if task is None:
            return RuntimeResult(
                None, None, None, None, None, None, None, "NO_ELIGIBLE_TASK", intake
            )
        if task.status is TaskStatus.WAITING_HUMAN:
            if not self._code_capable:
                return self._result(
                    task, self._latest_run(task.task_id), None, "CODE_CAPABILITY_MISSING", intake
                )
            run = self._latest_run(task.task_id)
            if run is None:
                return self._result(task, None, None, "WAITING_HUMAN", intake)
            # PublicationService recognizes the persisted PR and only reconciles
            # durable state; it does not commit, push or create a second PR.
            published = self._publication.publish(task.task_id, run.run_id)
            return self._result_from_publication(published, run, intake)

        if (
            task.kind is TaskKind.CODE
            and not self._code_capable
            and task.status not in {TaskStatus.DISCOVERED, TaskStatus.READY}
        ):
            return self._result(
                task, self._latest_run(task.task_id), None, "CODE_CAPABILITY_MISSING", intake
            )

        if (
            self._is_unstarted(task)
            and task.source is not None
            and not self._intake.is_eligible(
                Repository(task.source.repository_slug, role=self._intake_repository.role),
                task.source,
            )
        ):
            cancelled = self._dispatch.lifecycle.transition(task.task_id, TaskStatus.CANCELLED)
            return self._result(cancelled, None, None, "SOURCE_INELIGIBLE", intake)

        if task.kind is TaskKind.OPERATIONAL and self._is_unstarted(task) and task.source:
            current_source = self._intake.get_task(
                Repository(task.source.repository_slug, role=self._intake_repository.role),
                task.source,
            )
            if current_source.kind is not TaskKind.OPERATIONAL or current_source.body != task.body:
                cancelled = self._dispatch.lifecycle.transition(task.task_id, TaskStatus.CANCELLED)
                return self._result(cancelled, None, None, "SOURCE_DECLARATION_CHANGED", intake)

        if task.status is TaskStatus.DISCOVERED:
            task = self._dispatch.lifecycle.transition(task.task_id, TaskStatus.READY)
        if task.status is TaskStatus.CHANGES_REQUESTED:
            task = self._dispatch.lifecycle.transition(task.task_id, TaskStatus.READY)
        if task.status is TaskStatus.READY:
            if task.kind is TaskKind.OPERATIONAL:
                if not self._operational_policy.permits(OperationalCapability.SCRATCH):
                    blocked = self._block(task.task_id, "operational capability denied")
                    return self._result(blocked, None, None, "OPERATIONAL_POLICY_BLOCKED", intake)
                if self._operational_dispatch is None or self._adapter.kind is not AgentKind.CODEX:
                    blocked = self._block(task.task_id, "operational capability missing")
                    return self._result(
                        blocked, None, None, "OPERATIONAL_CAPABILITY_MISSING", intake
                    )
                try:
                    parse_scratch_artifact(task)
                except ValueError:
                    blocked = self._block(task.task_id, "operational policy rejected declaration")
                    return self._result(blocked, None, None, "OPERATIONAL_POLICY_BLOCKED", intake)
                run = self._operational_dispatch.dispatch(task.task_id, self._adapter)
            else:
                if not self._code_capable:
                    blocked = self._block(task.task_id, "code capability missing")
                    return self._result(blocked, None, None, "CODE_CAPABILITY_MISSING", intake)
                feedback = self._tasks.latest_rework_feedback(task.task_id)
                history = self._runs.list_runs(task.task_id)
                previous = history[-1] if history else None
                gate_source = previous
                if (
                    previous is not None
                    and previous.status is RunStatus.FAILED
                    and len(history) > 1
                    and history[-2].workspace == previous.workspace
                ):
                    gate_source = history[-2]
                gate_feedback = (
                    self._gate_feedback(gate_source)
                    if gate_source is not None
                    and gate_source.validation_outcome is ValidationOutcome.GATES_FAILED
                    else None
                )
                if (feedback is not None or gate_feedback is not None) and previous is not None:
                    if feedback is not None and self._adapter.kind is not AgentKind.CODEX:
                        return self._result(task, previous, None, "ENGINE_MISMATCH", intake)
                    run = self._dispatch.dispatch_rework(
                        task.task_id,
                        self._adapter,
                        "\n".join(part for part in (feedback, gate_feedback) if part),
                    )
                else:
                    run = self._dispatch.dispatch(task.task_id, self._adapter)
        else:
            run = self._runs.find_active_run(task.task_id)
            if run is None:
                if (
                    task.status is TaskStatus.CLAIMED
                    and self._tasks.latest_rework_feedback(task.task_id) is not None
                ):
                    return self._result(task, None, None, "RESUMABLE_STATE_MISSING_RUN", intake)
                run = self._latest_run(task.task_id)
            if run is None:
                return self._result(task, None, None, "RESUMABLE_STATE_MISSING_RUN", intake)

        if run.adapter is not self._adapter.kind:
            return self._result(task, run, None, "ENGINE_MISMATCH", intake)

        refresh = self._poll_until_terminal(run.run_id)
        if refresh is None:
            current = self._tasks.get(task.task_id) or task
            return self._result(current, run, None, "TIMEOUT_RESUMABLE", intake)
        current = self._tasks.get(task.task_id) or task
        if refresh.run.status is not RunStatus.SUCCEEDED:
            if task.kind is TaskKind.OPERATIONAL and current.status is TaskStatus.FAILED:
                current.blocked_reason = "operational agent execution failed"
                current = self._tasks.update(current)
            return self._result(current, refresh.run, refresh, "AGENT_NOT_SUCCESSFUL", intake)
        if refresh.outcome is not ValidationOutcome.READY_FOR_NEXT_PHASE:
            if task.kind is TaskKind.OPERATIONAL and current.status is TaskStatus.VALIDATING:
                current = self._block(task.task_id, "operational acceptance gates failed")
            elif task.kind is TaskKind.CODE and current.status is TaskStatus.VALIDATING:
                current = self._dispatch.lifecycle.transition(task.task_id, TaskStatus.READY)
            return self._result(current, refresh.run, refresh, "QUALITY_GATES_FAILED", intake)

        if task.kind is TaskKind.OPERATIONAL:
            if current.status is not TaskStatus.VALIDATING:
                return self._result(
                    current, refresh.run, refresh, "OPERATIONAL_STATE_INVALID", intake
                )
            current = self._dispatch.lifecycle.transition(task.task_id, TaskStatus.DONE)
            return self._result(current, refresh.run, refresh, "OPERATIONAL_DONE", intake)

        published = self._publication.publish(task.task_id, refresh.run.run_id)
        return self._result_from_publication(published, refresh.run, intake)

    def _reconcile_human_reviews(self) -> None:
        """Apply only provider-confirmed outcomes to durable human-review tasks."""
        if self._pull_request_state is None:
            return
        review_tasks = [
            *self._tasks.list(TaskStatus.WAITING_HUMAN),
            *self._tasks.list(TaskStatus.VALIDATING),
        ]
        for task in review_tasks:
            if self._sprint is not None and not self._sprint.allows_review(task):
                continue
            if task.kind is not TaskKind.CODE:
                continue
            run = self._latest_run(task.task_id)
            if run is None:
                continue
            pr = self._pull_requests.get_for_run(run.run_id)
            if pr is None and run.workspace is not None:
                pr = self._pull_requests.find_by_branch(
                    run.workspace.repository_slug, run.workspace.branch
                )
            if pr is None or pr.task_id != task.task_id:
                continue
            state = self._pull_request_state.state(pr)
            if state is PullRequestState.MERGED:
                self._dispatch.lifecycle.transition(task.task_id, TaskStatus.DONE)
            elif state is PullRequestState.CLOSED:
                self._dispatch.lifecycle.transition(task.task_id, TaskStatus.CANCELLED)

    def _select_task(self) -> FactoryTask | None:
        # Recovery states take precedence over new work; ordering within each
        # state is the repository's stable created_at/task_id order. Human
        # review of a CODE PR does not occupy the separate scratch workflow.
        requested = self._selectable(TaskStatus.CHANGES_REQUESTED)
        if requested:
            return requested[0]
        for ready in self._selectable(TaskStatus.READY):
            history = self._runs.list_runs(ready.task_id)
            previous = history[-1] if history else None
            gate_rework = previous is not None and (
                previous.validation_outcome is ValidationOutcome.GATES_FAILED
                or (
                    previous.status is RunStatus.FAILED
                    and len(history) > 1
                    and history[-2].workspace == previous.workspace
                    and history[-2].validation_outcome is ValidationOutcome.GATES_FAILED
                )
            )
            if previous is not None and (
                self._tasks.latest_rework_feedback(ready.task_id) or gate_rework
            ):
                return ready
        for status in (
            TaskStatus.CLAIMED,
            TaskStatus.RUNNING,
            TaskStatus.VALIDATING,
            TaskStatus.PR_OPEN,
        ):
            for candidate in self._selectable(status):
                if self._tasks.latest_rework_feedback(candidate.task_id):
                    return candidate
        waiting = self._selectable(TaskStatus.WAITING_HUMAN)
        if waiting:
            for status in (
                TaskStatus.VALIDATING,
                TaskStatus.RUNNING,
                TaskStatus.CLAIMED,
                TaskStatus.READY,
                TaskStatus.DISCOVERED,
            ):
                operational = next(
                    (
                        task
                        for task in self._selectable(status)
                        if task.kind is TaskKind.OPERATIONAL
                    ),
                    None,
                )
                if operational is not None:
                    return operational
            return waiting[0]
        for status in (
            TaskStatus.PR_OPEN,
            TaskStatus.VALIDATING,
            TaskStatus.RUNNING,
            TaskStatus.CLAIMED,
            TaskStatus.READY,
            TaskStatus.DISCOVERED,
        ):
            tasks = self._selectable(status)
            if tasks:
                return tasks[0]
        return None

    def _selectable(self, status: TaskStatus) -> list[FactoryTask]:
        return [
            task
            for task in self._tasks.list(status)
            if self._sprint is None or self._sprint.allows(task)
        ]

    def _block(self, task_id: str, reason: str) -> FactoryTask:
        blocked = self._dispatch.lifecycle.transition(task_id, TaskStatus.BLOCKED)
        blocked.blocked_reason = reason
        return self._tasks.update(blocked)

    def _is_unstarted(self, task: FactoryTask) -> bool:
        if task.status not in {TaskStatus.DISCOVERED, TaskStatus.READY}:
            return False
        if self._runs.list_runs(task.task_id):
            return False
        return all(
            transition.to_status is TaskStatus.READY
            for transition in self._tasks.history(task.task_id)
        )

    def _poll_until_terminal(self, run_id: str) -> RunRefresh | None:
        deadline = self._monotonic() + self._timeout
        while True:
            self._pulse_status()
            refresh = self._tracking.refresh(run_id, self._adapter)
            self._pulse_status()
            if refresh.run.is_terminal:
                return refresh
            if self._monotonic() >= deadline:
                return None
            self._sleep(min(self._poll_interval, max(0.0, deadline - self._monotonic())))

    def _pulse_status(self) -> None:
        if self._status_pulse is None:
            return
        with suppress(Exception):  # delivery failure leaves events queued
            self._status_pulse()

    def _latest_run(self, task_id: str) -> AgentRun | None:
        runs = self._runs.list_runs(task_id)
        return runs[-1] if runs else None

    @staticmethod
    def _gate_feedback(run: AgentRun) -> str:
        """Give the coding agent bounded, allowlisted QA results for correction."""
        failed = [
            gate.name if re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", gate.name) else "check"
            for gate in run.gates
            if gate.is_blocking
        ]
        names = ", ".join(failed[:20])
        return f"Factory quality gates failed: {names}. Fix the failures and rerun validation."

    @staticmethod
    def _result(
        task: FactoryTask,
        run: AgentRun | None,
        refresh: RunRefresh | None,
        outcome: str,
        intake: IntakeSummary,
    ) -> RuntimeResult:
        workspace = run.workspace if run is not None else None
        return RuntimeResult(
            task_id=task.task_id,
            run_id=run.run_id if run is not None else None,
            task_status=task.status,
            branch=(workspace.branch or None) if workspace is not None else None,
            validation=refresh.outcome
            if refresh is not None
            else (run.validation_outcome if run is not None else None),
            pull_request_number=None,
            pull_request_url=None,
            outcome=outcome,
            intake=intake,
        )

    @staticmethod
    def _result_from_publication(
        published: PublicationResult, run: AgentRun, intake: IntakeSummary
    ) -> RuntimeResult:
        return RuntimeResult(
            task_id=run.task_id,
            run_id=run.run_id,
            task_status=published.task_status,
            branch=published.pull_request.head_branch,
            validation=run.validation_outcome,
            pull_request_number=published.pull_request.number,
            pull_request_url=published.pull_request.url,
            outcome="WAITING_HUMAN",
            intake=intake,
        )


__all__ = ["FactoryRuntime", "RuntimeResult"]
