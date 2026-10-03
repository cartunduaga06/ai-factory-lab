"""One-shot, resumable runtime for the already implemented factory phases."""

from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime

from factory.domain.enums import (
    AgentKind,
    QualityGateStatus,
    RunStatus,
    TaskKind,
    TaskStatus,
    ValidationOutcome,
)
from factory.domain.errors import AgentCollectError
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    FactoryTask,
    QualityGate,
    QualityGateSpec,
    Repository,
)
from factory.domain.operational import (
    OperationalCapability,
    OperationalPolicy,
    parse_database_readonly,
    parse_docker_inspect,
    parse_scratch_artifact,
    parse_service_health,
)
from factory.domain.ports import (
    BacklogLinkRepository,
    DatabaseReadonlyInspector,
    DockerInspector,
    OperationalAcceptance,
    PullRequestRepository,
    PullRequestSink,
    PullRequestState,
    PullRequestStateSource,
    QualityGateRunner,
    RunRepository,
    SecurityReviewGate,
    ServiceHealthChecker,
    TaskRepository,
    WorkspaceProvisioner,
    WorkspacePublisher,
    WorkspaceRevisionInspector,
)
from factory.domain.projects import ProjectRegistry, ProjectRoutingError
from factory.orchestration.context import ContextBuildError, ContextPackBuilder
from factory.orchestration.dispatch import DispatchService
from factory.orchestration.feedback import FeedbackReconciliationService
from factory.orchestration.intake import IntakeSummary, IssueIntakeService
from factory.orchestration.publication import (
    PublicationResult,
    PublicationService,
    SecurityReviewBlocked,
)
from factory.orchestration.reconciliation import ReconciliationService
from factory.orchestration.recovery import FailureClass, RecoveryPolicy
from factory.orchestration.sprint import SprintService
from factory.orchestration.tracking import RunRefresh, RunTrackingService

logger = logging.getLogger(__name__)


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
        security_review: SecurityReviewGate,
        pull_request_state: PullRequestStateSource | None = None,
        operational_policy: OperationalPolicy | None = None,
        operational_provisioner: WorkspaceProvisioner | None = None,
        operational_root: str | None = None,
        operational_acceptance: OperationalAcceptance | None = None,
        database_inspector: DatabaseReadonlyInspector | None = None,
        service_health_checker: ServiceHealthChecker | None = None,
        docker_inspector: DockerInspector | None = None,
        code_capable: bool = True,
        poll_interval: float = 5.0,
        timeout: float = 1800.0,
        heartbeat_interval: float = 180.0,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        status_pulse: Callable[[], None] | None = None,
        backlog_reconcile: Callable[[], object] | None = None,
        sprint: SprintService | None = None,
        recovery_policy: RecoveryPolicy | None = None,
        context_builder: ContextPackBuilder | None = None,
        registry: ProjectRegistry | None = None,
        feedback: FeedbackReconciliationService | None = None,
        pool_mode: bool = False,
        pool_backlog_links: BacklogLinkRepository | None = None,
        owner_report: Callable[[str], None] | None = None,
    ) -> None:
        self._intake = intake
        self._intake_repository = intake_repository
        self._registry = registry
        self._feedback = feedback
        self._pool_backlog_links = pool_backlog_links
        self._tasks = tasks
        self._runs = runs
        self._adapter = adapter
        self._dispatch = DispatchService(
            tasks,
            runs,
            provisioner=provisioner,
            workspace_root=workspace_root,
            context_builder=context_builder,
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
            registry=registry,
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
            registry=registry,
            security_review=security_review,
        )
        self._reconciliation = ReconciliationService(
            tasks,
            runs,
            self._tracking,
            adapter,
            allows=self._pool_allows
            if pool_mode and sprint
            else (sprint.allows if sprint else None),
            registry=registry,
        )
        self._pull_requests = pull_requests
        self._pull_request_state = pull_request_state
        self._operational_policy = operational_policy or OperationalPolicy()
        self._database_inspector = database_inspector
        self._service_health_checker = service_health_checker
        self._docker_inspector = docker_inspector
        self._poll_interval = max(0.0, poll_interval)
        self._timeout = max(0.0, timeout)
        self._sleep = sleep
        self._monotonic = monotonic
        self._status_pulse = status_pulse
        self._backlog_reconcile = backlog_reconcile
        self._sprint = sprint
        self._recovery_policy = recovery_policy or RecoveryPolicy()
        self._pool_mode = pool_mode
        self._owner_report = owner_report

    def run_once(self) -> RuntimeResult:
        """Run intake and reconcile exactly one task, never merging or deploying."""
        try:
            self._reconciliation.reconcile()
            self._reconcile_human_reviews()
            if self._sprint is not None and self._sprint.is_paused():
                self._sprint.resume_completed()
                if self._sprint.is_paused():
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
            if result.task_id is not None and self._owner_report is not None:
                self._publish_owner_report(result.task_id)
            return result
        finally:
            self._pulse_status()

    def prepare_pool(self) -> IntakeSummary:
        """Refresh durable state and intake once before a pool scheduling pass."""
        self._reconciliation.reconcile()
        self._reconcile_human_reviews()
        if self._sprint is not None:
            self._sprint.resume_completed()
            self._sprint.prepare()
        if self._should_reconcile_backlog():
            assert self._backlog_reconcile is not None
            self._backlog_reconcile()
        before = {task.task_id for task in self._tasks.list()} if self._owner_report else set()
        summary = self._intake.intake(self._intake_repository)
        if self._owner_report is not None and summary.created:
            for task in self._tasks.list():
                if (
                    task.task_id not in before
                    and task.source is not None
                    and task.source.provider == "github"
                ):
                    self._publish_owner_report(task.task_id)
        return summary

    def pool_candidates(self) -> tuple[str, ...]:
        """Return unfinished task identities in recovery-first order."""
        statuses = (
            TaskStatus.PR_OPEN,
            TaskStatus.VALIDATING,
            TaskStatus.RUNNING,
            TaskStatus.CLAIMED,
            TaskStatus.CHANGES_REQUESTED,
            TaskStatus.READY,
            TaskStatus.DISCOVERED,
        )
        return tuple(
            task.task_id for status in statuses for task in self._selectable(status, pool=True)
        )

    def run_task(self, task_id: str) -> RuntimeResult:
        """Resume exactly one named task; dispatch still claims it atomically."""
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if not self._pool_allows(task):
            raise ValueError("task is not authorized for pool execution")
        try:
            result = self._run_once(selected_task=task, do_intake=False)
            if self._owner_report is not None:
                self._publish_owner_report(task_id)
            return result
        finally:
            self._pulse_status()

    def _publish_owner_report(self, task_id: str) -> None:
        """Keep a provider feedback outage from changing task execution state."""
        if self._owner_report is None:
            return
        try:
            self._owner_report(task_id)
        except Exception as exc:  # noqa: BLE001 - provider text must not enter logs
            logger.warning(
                "owner report update deferred: task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
            )

    def _run_once(
        self, selected_task: FactoryTask | None = None, *, do_intake: bool = True
    ) -> RuntimeResult:
        """Drive the existing one-shot lifecycle."""
        if do_intake and self._should_reconcile_backlog():
            assert self._backlog_reconcile is not None
            self._backlog_reconcile()
        before = (
            {task.task_id for task in self._tasks.list()}
            if do_intake and self._owner_report
            else set()
        )
        intake = self._intake.intake(self._intake_repository) if do_intake else IntakeSummary()
        if self._owner_report is not None and intake.created:
            for candidate in self._tasks.list():
                if (
                    candidate.task_id not in before
                    and candidate.source is not None
                    and candidate.source.provider == "github"
                ):
                    self._publish_owner_report(candidate.task_id)
        if do_intake:
            self._reconcile_human_reviews()
        task = selected_task or self._select_task()
        if task is None:
            return RuntimeResult(
                None, None, None, None, None, None, None, "NO_ELIGIBLE_TASK", intake
            )
        if self._registry is not None and task.kind is TaskKind.CODE:
            try:
                self._registry.resolve(task.project_id, task.target_repository)
            except ProjectRoutingError:
                blocked = (
                    self._block(task.task_id, "project routing rejected")
                    if task.status
                    in {
                        TaskStatus.READY,
                        TaskStatus.CLAIMED,
                        TaskStatus.RUNNING,
                        TaskStatus.VALIDATING,
                    }
                    else task
                )
                return self._result(
                    blocked,
                    self._latest_run(task.task_id),
                    None,
                    "PROJECT_ROUTING_REJECTED",
                    intake,
                )
        if task.status in {TaskStatus.DISCOVERED, TaskStatus.READY, TaskStatus.CHANGES_REQUESTED}:
            active = self._runs.find_active_run(task.task_id)
            if active is not None:
                return self._result(task, active, None, "ACTIVE_RUN_STATE_MISMATCH", intake)
            busy = (
                do_intake
                and not self._pool_mode
                and any(
                    other.task_id != task.task_id
                    for status in (
                        TaskStatus.CLAIMED,
                        TaskStatus.RUNNING,
                        TaskStatus.VALIDATING,
                        TaskStatus.PR_OPEN,
                    )
                    for other in self._tasks.list(status)
                )
            )
            busy = busy or (
                do_intake
                and not self._pool_mode
                and any(
                    run.task_id != task.task_id and not run.is_terminal
                    for run in self._runs.list_runs()
                )
            )
            if busy:
                return self._result(task, self._latest_run(task.task_id), None, "WIP_BUSY", intake)
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
                try:
                    target_id = parse_database_readonly(task.body)
                except ValueError:
                    target_id = None
                if target_id is not None:
                    return self._run_database(task, target_id, intake)
                try:
                    service_target_id = parse_service_health(task.body)
                except ValueError:
                    service_target_id = None
                if service_target_id is not None:
                    return self._run_service_health(task, service_target_id, intake)
                try:
                    docker_target_id = parse_docker_inspect(task.body)
                except ValueError:
                    docker_target_id = None
                if docker_target_id is not None:
                    return self._run_docker(task, docker_target_id, intake)
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
                if gate_feedback is not None and previous is not None:
                    last_attempt = previous.finished_at or previous.started_at
                    if last_attempt is None:
                        return self._result(task, previous, None, "BACKOFF_PENDING", intake)
                    elapsed = (datetime.now(UTC) - last_attempt).total_seconds()
                    delay = self._recovery_policy.delay_for(
                        max(1, self._gate_failure_streak(task.task_id))
                    )
                    if elapsed < delay:
                        return self._result(task, previous, None, "BACKOFF_PENDING", intake)
                # An operator-authorized terminal timeout is exceptional:
                # reuse the exact failed run's checkout instead of discarding
                # partially written code. The prior FAILED run stays immutable.
                timeout_recovery = (
                    previous is not None
                    and previous.status is RunStatus.FAILED
                    and task.blocked_reason == f"terminal-timeout-recovery:{previous.run_id}"
                )
                if timeout_recovery:
                    if self._adapter.kind is not AgentKind.CODEX:
                        return self._result(task, previous, None, "ENGINE_MISMATCH", intake)
                    try:
                        run = self._dispatch.dispatch_rework(
                            task.task_id,
                            self._adapter,
                            "Operator-authorized timeout recovery: resume the original "
                            "workspace without erasing existing changes. Complete the "
                            "original task and required tests; stop at human PR review.",
                        )
                    except ContextBuildError:
                        return self._context_blocked(task, previous, intake)
                elif (feedback is not None or gate_feedback is not None) and previous is not None:
                    if feedback is not None and self._adapter.kind is not AgentKind.CODEX:
                        return self._result(task, previous, None, "ENGINE_MISMATCH", intake)
                    try:
                        run = self._dispatch.dispatch_rework(
                            task.task_id,
                            self._adapter,
                            "\n".join(part for part in (feedback, gate_feedback) if part),
                        )
                    except ContextBuildError:
                        return self._context_blocked(task, previous, intake)
                else:
                    try:
                        run = self._dispatch.dispatch(task.task_id, self._adapter)
                    except ContextBuildError:
                        return self._context_blocked(task, previous, intake)
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

        if run.project_id != task.project_id or (
            run.workspace is not None and run.workspace.repository_slug != task.target_repository
        ):
            return self._result(task, run, None, "PROJECT_ROUTING_REJECTED", intake)
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
                if (
                    self._recovery_policy.classify(refresh.run) is FailureClass.CORRECTABLE
                    and self._gate_failure_streak(task.task_id)
                    > self._recovery_policy.correction_limit
                ):
                    current = self._block(task.task_id, "quality correction limit reached")
                else:
                    current = self._dispatch.lifecycle.transition(task.task_id, TaskStatus.READY)
            return self._result(current, refresh.run, refresh, "QUALITY_GATES_FAILED", intake)

        if task.kind is TaskKind.OPERATIONAL:
            if current.status is not TaskStatus.VALIDATING:
                return self._result(
                    current, refresh.run, refresh, "OPERATIONAL_STATE_INVALID", intake
                )
            current = self._dispatch.lifecycle.transition(task.task_id, TaskStatus.DONE)
            return self._result(current, refresh.run, refresh, "OPERATIONAL_DONE", intake)

        try:
            published = self._publication.publish(task.task_id, refresh.run.run_id)
        except SecurityReviewBlocked:
            current = self._block(task.task_id, "security review blocked publication")
            return self._result(current, refresh.run, refresh, "SECURITY_REVIEW_BLOCKED", intake)
        except Exception:
            if self._pool_mode:
                self._block_failed_publication(task.task_id, refresh.run)
            raise
        return self._result_from_publication(published, refresh.run, intake)

    def _block_failed_publication(self, task_id: str, run: AgentRun) -> None:
        """Keep a failed pool publication out of automatic restart selection."""
        task = self._tasks.get(task_id)
        workspace = run.workspace
        latest = self._latest_run(task_id)
        if (
            task is None
            or task.status is not TaskStatus.VALIDATING
            or workspace is None
            or latest is None
            or latest.run_id != run.run_id
            or latest.status is not RunStatus.SUCCEEDED
            or latest.validation_outcome is not ValidationOutcome.READY_FOR_NEXT_PHASE
            or latest.workspace is None
            or latest.workspace.workspace_id != workspace.workspace_id
            or self._pull_requests.get_for_run(run.run_id) is not None
        ):
            return
        self._block(
            task_id,
            f"publication failed: run {run.run_id}, workspace {workspace.workspace_id}",
        )

    def _run_database(
        self, task: FactoryTask, target_id: str, intake: IntakeSummary
    ) -> RuntimeResult:
        """Run an approved fixed observation directly, without agent dispatch."""
        if self._database_inspector is None or not self._operational_policy.permits(
            OperationalCapability.DATABASE_READONLY,
            host="local",
            path=self._database_inspector.target_path(target_id),
            command="inspect",
            target=target_id,
        ):
            blocked = self._block(task.task_id, "operational capability denied")
            return self._result(blocked, None, None, "OPERATIONAL_POLICY_BLOCKED", intake)
        lifecycle = self._dispatch.lifecycle
        lifecycle.transition(task.task_id, TaskStatus.CLAIMED)
        lifecycle.transition(task.task_id, TaskStatus.RUNNING)
        run = self._runs.save_run(
            AgentRun(
                task_id=task.task_id,
                adapter=AgentKind.OTHER,
                status=RunStatus.RUNNING,
                started_at=datetime.now(UTC),
                project_id=task.project_id,
            )
        )
        try:
            evidence = self._database_inspector.inspect(target_id)
        except ValueError:
            run.status = RunStatus.FAILED
            run.summary = "database inspection rejected"
            run.gates = (QualityGate("database_readonly", QualityGateStatus.FAILED),)
            outcome = "OPERATIONAL_DATABASE_BLOCKED"
        else:
            run.status = RunStatus.SUCCEEDED
            run.summary = evidence
            run.gates = (QualityGate("database_readonly", QualityGateStatus.PASSED),)
            outcome = "OPERATIONAL_DONE"
        run.finished_at = datetime.now(UTC)
        self._runs.update_run(run)
        lifecycle.transition(task.task_id, TaskStatus.VALIDATING)
        current = (
            lifecycle.transition(task.task_id, TaskStatus.DONE)
            if run.status is RunStatus.SUCCEEDED
            else self._block(task.task_id, "database inspection rejected")
        )
        return self._result(current, run, None, outcome, intake)

    def _run_service_health(
        self, task: FactoryTask, target_id: str, intake: IntakeSummary
    ) -> RuntimeResult:
        """Run one allowlisted read-only service probe without agent dispatch."""
        checker = self._service_health_checker
        if checker is None or not self._operational_policy.permits(
            OperationalCapability.SERVICE_HEALTH,
            host="https",
            path=checker.target_url(target_id),
            command="get",
            target=target_id,
        ):
            blocked = self._block(task.task_id, "operational capability denied")
            return self._result(blocked, None, None, "OPERATIONAL_POLICY_BLOCKED", intake)
        lifecycle = self._dispatch.lifecycle
        lifecycle.transition(task.task_id, TaskStatus.CLAIMED)
        lifecycle.transition(task.task_id, TaskStatus.RUNNING)
        run = self._runs.save_run(
            AgentRun(
                task_id=task.task_id,
                adapter=AgentKind.OTHER,
                status=RunStatus.RUNNING,
                started_at=datetime.now(UTC),
                project_id=task.project_id,
            )
        )
        evidence = checker.check(target_id)
        try:
            parsed = json.loads(evidence)
            healthy = (
                isinstance(parsed, dict)
                and parsed.get("target_id") == target_id
                and parsed.get("status") in {"HEALTHY", "WARNING", "CRITICAL", "UNKNOWN"}
                and isinstance(parsed.get("observed_at"), str)
                and isinstance(parsed.get("evidence"), str)
            )
        except (ValueError, TypeError):
            healthy = False
        run.status = RunStatus.SUCCEEDED if healthy else RunStatus.FAILED
        run.summary = evidence if healthy else '{"status":"UNKNOWN","evidence":"invalid_result"}'
        run.gates = (
            QualityGate(
                "service_health",
                QualityGateStatus.PASSED if healthy else QualityGateStatus.FAILED,
            ),
        )
        return self._finish_operational_observation(
            task,
            run,
            intake,
            healthy,
            "OPERATIONAL_SERVICE_BLOCKED",
            "service health observation failed",
        )

    def _run_docker(
        self, task: FactoryTask, target_id: str, intake: IntakeSummary
    ) -> RuntimeResult:
        """Run an approved fixed Docker observation directly, without agent dispatch."""
        inspector = self._docker_inspector
        if (
            inspector is None
            or not self._operational_policy.permits(
                OperationalCapability.DOCKER_INSPECT,
                host="local",
                path="docker-engine-api",
                command="inspect",
                target=target_id,
            )
            or not inspector.registered_container(target_id)
        ):
            blocked = self._block(task.task_id, "operational capability denied")
            return self._result(blocked, None, None, "OPERATIONAL_POLICY_BLOCKED", intake)
        lifecycle = self._dispatch.lifecycle
        lifecycle.transition(task.task_id, TaskStatus.CLAIMED)
        lifecycle.transition(task.task_id, TaskStatus.RUNNING)
        run = self._runs.save_run(
            AgentRun(
                task_id=task.task_id,
                adapter=AgentKind.OTHER,
                status=RunStatus.RUNNING,
                started_at=datetime.now(UTC),
                project_id=task.project_id,
            )
        )
        try:
            evidence = inspector.inspect(target_id)
        except ValueError:
            run.status = RunStatus.FAILED
            run.summary = "Docker inspection rejected"
            run.gates = (QualityGate("docker_inspect", QualityGateStatus.FAILED),)
            succeeded = False
        else:
            run.status = RunStatus.SUCCEEDED
            run.summary = evidence
            run.gates = (QualityGate("docker_inspect", QualityGateStatus.PASSED),)
            succeeded = True
        return self._finish_operational_observation(
            task, run, intake, succeeded, "OPERATIONAL_DOCKER_BLOCKED", "Docker inspection rejected"
        )

    def _finish_operational_observation(
        self,
        task: FactoryTask,
        run: AgentRun,
        intake: IntakeSummary,
        succeeded: bool,
        failure_outcome: str,
        failure_reason: str,
    ) -> RuntimeResult:
        """Persist a direct operational observation and close its lifecycle."""
        run.finished_at = datetime.now(UTC)
        self._runs.update_run(run)
        lifecycle = self._dispatch.lifecycle
        lifecycle.transition(task.task_id, TaskStatus.VALIDATING)
        current = (
            lifecycle.transition(task.task_id, TaskStatus.DONE)
            if succeeded
            else self._block(task.task_id, failure_reason)
        )
        return self._result(
            current, run, None, "OPERATIONAL_DONE" if succeeded else failure_outcome, intake
        )

    def _reconcile_human_reviews(self) -> None:
        """Apply only provider-confirmed outcomes to durable human-review tasks."""
        if self._pull_request_state is None:
            return
        review_tasks = [
            *self._tasks.list(TaskStatus.WAITING_HUMAN),
            *self._tasks.list(TaskStatus.VALIDATING),
            *self._tasks.list(TaskStatus.DONE),
            *self._tasks.list(TaskStatus.FAILED),
            *self._tasks.list(TaskStatus.CANCELLED),
        ]
        for task in review_tasks:
            if self._registry is not None:
                try:
                    self._registry.resolve(task.project_id, task.target_repository)
                except ProjectRoutingError:
                    continue
            if task.kind is not TaskKind.CODE:
                continue
            run = self._latest_run(task.task_id)
            if run is None or run.project_id != task.project_id:
                continue
            pr = self._pull_requests.get_for_run(run.run_id)
            if pr is None and run.workspace is not None:
                pr = self._pull_requests.find_by_branch(
                    run.workspace.repository_slug, run.workspace.branch
                )
            if (
                pr is None
                or pr.task_id != task.task_id
                or pr.repository_slug != task.target_repository
            ):
                continue
            if task.status is TaskStatus.CANCELLED:
                if self._feedback is not None:
                    self._feedback.reconcile_provider_closure(task, run)
                continue
            try:
                state = self._pull_request_state.state(pr)
            except Exception:
                if self._feedback is not None:
                    self._feedback.reconcile_provider_closure(task, run)
                    continue
                raise
            if state is PullRequestState.MERGED:
                if self._feedback is not None:
                    self._feedback.reconcile_external(task, run)
                elif task.status in {TaskStatus.WAITING_HUMAN, TaskStatus.VALIDATING}:
                    self._dispatch.lifecycle.transition(task.task_id, TaskStatus.DONE)
            elif state is PullRequestState.CLOSED and task.status in {
                TaskStatus.WAITING_HUMAN,
                TaskStatus.VALIDATING,
            }:
                cancelled = self._dispatch.lifecycle.transition(task.task_id, TaskStatus.CANCELLED)
                cancelled.blocked_reason = (
                    "pull request closed without merge; human review required"
                )
                self._tasks.update(cancelled)
                if self._feedback is not None:
                    self._feedback.sync(task, run, "CLOSED")
            elif self._feedback is not None and task.status in {
                TaskStatus.WAITING_HUMAN,
                TaskStatus.FAILED,
                TaskStatus.CANCELLED,
            }:
                self._feedback.sync(task, run, task.status.value)
            if self._feedback is not None:
                self._feedback.reconcile_provider_closure(task, run)

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

    def _pool_allows(self, task: FactoryTask) -> bool:
        if self._sprint is None:
            return True
        provenance = (
            self._pool_backlog_links.reconciliation_origin(task.task_id)
            if self._pool_backlog_links is not None
            else None
        )
        return (
            self._sprint.allows(task)
            or (
                not self._sprint.has_live_sprint()
                and task.source is not None
                and task.source.provider == "github"
                and provenance is not None
                and provenance[0] == "trello"
            )
            or (
                task.source is not None
                and task.source.provider == "github"
                and task.source.repository_slug == task.target_repository
                and self._pool_backlog_links is not None
                and provenance == ("github-direct", None)
            )
        )

    def _should_reconcile_backlog(self) -> bool:
        return self._backlog_reconcile is not None and (
            self._sprint is None or not self._sprint.has_live_sprint()
        )

    def _selectable(self, status: TaskStatus, *, pool: bool = False) -> list[FactoryTask]:
        return [
            task
            for task in self._tasks.list(status)
            if (
                self._pool_allows(task)
                if pool
                else self._sprint is None or self._sprint.allows(task)
            )
        ]

    def _block(self, task_id: str, reason: str) -> FactoryTask:
        blocked = self._dispatch.lifecycle.transition(task_id, TaskStatus.BLOCKED)
        blocked.blocked_reason = reason
        return self._tasks.update(blocked)

    def _context_blocked(
        self, task: FactoryTask, previous: AgentRun | None, intake: IntakeSummary
    ) -> RuntimeResult:
        blocked = self._tasks.get(task.task_id)
        assert blocked is not None
        return self._result(blocked, previous, None, "REQUIRED_CONTEXT_BLOCKED", intake)

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
            try:
                refresh = self._tracking.refresh(run_id, self._adapter)
            except AgentCollectError:
                # The engine may still be executing. Preserve the active run and
                # its global claim, then observe the same run on the next pass.
                return None
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

    def _gate_failure_streak(self, task_id: str) -> int:
        streak = 0
        for run in reversed(self._runs.list_runs(task_id)):
            if run.validation_outcome is not ValidationOutcome.GATES_FAILED:
                break
            streak += 1
        return streak

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
