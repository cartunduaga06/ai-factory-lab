"""One-shot, resumable runtime for the already implemented factory phases."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from factory.domain.enums import AgentKind, RunStatus, TaskKind, TaskStatus, ValidationOutcome
from factory.domain.models import AgentAdapter, AgentRun, FactoryTask, QualityGateSpec, Repository
from factory.domain.operational import parse_scratch_artifact
from factory.domain.ports import (
    OperationalAcceptance,
    PullRequestRepository,
    PullRequestSink,
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
        gate_runner: QualityGateRunner,
        revision_inspector: WorkspaceRevisionInspector,
        publisher: WorkspacePublisher,
        pull_request_sink: PullRequestSink,
        pull_requests: PullRequestRepository,
        base_branch: str,
        operational_provisioner: WorkspaceProvisioner | None = None,
        operational_root: str | None = None,
        operational_acceptance: OperationalAcceptance | None = None,
        code_capable: bool = True,
        poll_interval: float = 5.0,
        timeout: float = 1800.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
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
            gate_runner=gate_runner,
            revision_inspector=revision_inspector,
            provisioner=provisioner,
            pull_requests=pull_requests,
            pull_request_sink=pull_request_sink,
            base_branch=base_branch,
            operational_acceptance=operational_acceptance,
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
        self._poll_interval = max(0.0, poll_interval)
        self._timeout = max(0.0, timeout)
        self._sleep = sleep
        self._monotonic = monotonic

    def run_once(self) -> RuntimeResult:
        """Run intake and reconcile exactly one task, never merging or deploying."""
        intake = self._intake.intake(self._intake_repository)
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
        if task.status is TaskStatus.READY:
            if task.kind is TaskKind.OPERATIONAL:
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
                run = self._dispatch.dispatch(task.task_id, self._adapter)
        else:
            run = self._runs.find_active_run(task.task_id)
            if run is None:
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

    def _select_task(self) -> FactoryTask | None:
        # Recovery states take precedence over new work; ordering within each
        # state is the repository's stable created_at/task_id order.
        for status in (
            TaskStatus.WAITING_HUMAN,
            TaskStatus.PR_OPEN,
            TaskStatus.VALIDATING,
            TaskStatus.RUNNING,
            TaskStatus.CLAIMED,
            TaskStatus.READY,
            TaskStatus.DISCOVERED,
        ):
            tasks = self._tasks.list(status)
            if tasks:
                return tasks[0]
        return None

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
            refresh = self._tracking.refresh(run_id, self._adapter)
            if refresh.run.is_terminal:
                return refresh
            if self._monotonic() >= deadline:
                return None
            self._sleep(min(self._poll_interval, max(0.0, deadline - self._monotonic())))

    def _latest_run(self, task_id: str) -> AgentRun | None:
        runs = self._runs.list_runs(task_id)
        return runs[-1] if runs else None

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
