"""First-class deterministic post-rebase revalidation orchestration."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus, TaskStatus
from factory.domain.errors import FactoryError, TaskStateChangedError
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PullRequest,
    QualityGate,
    QualityGateSpec,
    Workspace,
)
from factory.domain.ports import (
    PostRebaseEvidenceSource,
    PostRebaseRevalidationRepository,
    PullRequestRepository,
    QualityGateRunner,
    RunRepository,
    SecurityInspector,
    TaskRepository,
    WorkspaceRevalidationInspector,
)
from factory.domain.projects import ProjectRegistry
from factory.domain.revalidation import (
    ExactHeadCiStatus,
    PostRebaseRevalidation,
    RevalidationResult,
)
from factory.orchestration.transitions import TaskLifecycleService

REVALIDATION_BLOCK_REASON = "pull request head changed; revalidation required"


@dataclass(frozen=True, slots=True)
class RevalidationOutcome:
    """One bounded revalidation invocation; CI waiting is not an error."""

    attempt: PostRebaseRevalidation
    task_status: TaskStatus


class PostRebaseRevalidationService:
    """Create fresh evidence for an externally rebased, already-open PR.

    Agent runs are historical provenance only. This service never writes them.
    """

    def __init__(
        self,
        tasks: TaskRepository,
        runs: RunRepository,
        pull_requests: PullRequestRepository,
        attempts: PostRebaseRevalidationRepository,
        gate_runner: QualityGateRunner,
        workspace_inspector: WorkspaceRevalidationInspector,
        security_inspector: SecurityInspector,
        provider: PostRebaseEvidenceSource,
        *,
        registry: ProjectRegistry | None = None,
        gate_specs: Sequence[QualityGateSpec] = (),
        task_gate_specs: Mapping[str, Sequence[QualityGateSpec]] | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._pull_requests = pull_requests
        self._attempts = attempts
        self._gate_runner = gate_runner
        self._workspace_inspector = workspace_inspector
        self._security_inspector = security_inspector
        self._provider = provider
        self._registry = registry
        self._gate_specs = tuple(gate_specs)
        self._task_gate_specs = {
            ref: tuple(specs) for ref, specs in (task_gate_specs or {}).items()
        }
        self._lifecycle = TaskLifecycleService(tasks)

    def revalidate(self, task_id: str) -> RevalidationOutcome:
        task = self._require_task(task_id)
        active = self._attempts.active_for_task(task_id)
        run, pull_request = self._source(task)

        if active is None:
            latest = self._attempts.latest_for_task(task_id)
            if (
                task.status is TaskStatus.WAITING_HUMAN
                and latest is not None
                and latest.result is RevalidationResult.PASSED
                and latest.source_run_id == run.run_id
                and pull_request.commit_sha == latest.new_head
            ):
                return RevalidationOutcome(latest, task.status)
            self._require_revalidation_block(task)
            previous = pull_request.commit_sha
            if not previous:
                raise ValueError("persisted pull request has no exact head")
            new_head = self._provider.current_head(pull_request)
            if new_head == previous:
                raise ValueError("pull request head did not change")
            active = self._attempts.start(
                PostRebaseRevalidation(
                    task_id=task.task_id,
                    source_run_id=run.run_id,
                    repository_slug=pull_request.repository_slug,
                    head_branch=pull_request.head_branch,
                    pull_request_number=pull_request.number or 0,
                    previous_head=previous,
                    new_head=new_head,
                )
            )
        else:
            self._require_attempt_identity(active, task, run, pull_request)
            if task.status not in {TaskStatus.BLOCKED, TaskStatus.WAITING_HUMAN}:
                raise ValueError("revalidation task is not recoverable")

        if active.result is RevalidationResult.PENDING:
            active = self._local_validate(task, run, active)
        if active.result is RevalidationResult.WAITING_CI:
            active = self._provider_validate_and_rebind(task, run, pull_request, active)

        current = self._require_task(task_id)
        return RevalidationOutcome(active, current.status)

    def _local_validate(
        self, task: FactoryTask, run: AgentRun, attempt: PostRebaseRevalidation
    ) -> PostRebaseRevalidation:
        workspace = run.workspace
        if workspace is None:
            return self._attempts.mark_failed(attempt.attempt_id, "workspace_unavailable")
        try:
            before = self._workspace_inspector.snapshot(workspace)
        except Exception:  # noqa: BLE001 - adapter details stay private
            return self._attempts.mark_failed(attempt.attempt_id, "workspace_unavailable")
        if before.head_sha != attempt.new_head:
            return self._attempts.mark_failed(attempt.attempt_id, "workspace_head_mismatch")

        gates = tuple(self._run_gate(spec, workspace) for spec in self._specs_for(task))
        try:
            after = self._workspace_inspector.snapshot(workspace)
        except Exception:  # noqa: BLE001 - adapter details stay private
            return self._attempts.mark_failed(attempt.attempt_id, "workspace_changed", gates=gates)
        if before != after:
            return self._attempts.mark_failed(attempt.attempt_id, "workspace_changed", gates=gates)
        if any(gate.is_blocking for gate in gates):
            return self._attempts.mark_failed(
                attempt.attempt_id, "quality_gates_failed", gates=gates
            )

        review_run = AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            run_id=attempt.attempt_id,
            status=RunStatus.SUCCEEDED,
            workspace=workspace,
            gates=gates,
            validated_revision=after.tree_sha,
            project_id=task.project_id,
        )
        try:
            review = self._security_inspector.inspect(task, review_run)
        except Exception:  # noqa: BLE001 - scanner details stay private
            return self._attempts.mark_failed(
                attempt.attempt_id,
                "security_review_unavailable",
                validated_tree=after.tree_sha,
                gates=gates,
            )
        if review.critical:
            return self._attempts.mark_failed(
                attempt.attempt_id,
                "security_review_failed",
                validated_tree=after.tree_sha,
                gates=gates,
                security_review=review,
            )
        return self._attempts.record_local_validation(
            attempt.attempt_id, after.tree_sha, gates, review
        )

    def _provider_validate_and_rebind(
        self,
        task: FactoryTask,
        run: AgentRun,
        pull_request: PullRequest,
        attempt: PostRebaseRevalidation,
    ) -> PostRebaseRevalidation:
        workspace = run.workspace
        if workspace is None or attempt.validated_tree is None:
            return self._attempts.mark_failed(attempt.attempt_id, "local_evidence_missing")
        try:
            snapshot = self._workspace_inspector.snapshot(workspace)
        except Exception:  # noqa: BLE001
            return self._attempts.mark_failed(attempt.attempt_id, "workspace_unavailable")
        if snapshot.head_sha != attempt.new_head or snapshot.tree_sha != attempt.validated_tree:
            return self._attempts.mark_failed(attempt.attempt_id, "workspace_changed")

        required_ci, checks = self._ci_policy(task)
        try:
            evidence = self._provider.exact_ci(
                pull_request,
                attempt.new_head,
                checks,
                required=required_ci,
            )
        except Exception:  # noqa: BLE001 - provider detail is untrusted
            return self._attempts.mark_failed(attempt.attempt_id, "provider_head_changed")
        if evidence.status is ExactHeadCiStatus.PENDING:
            return attempt
        if evidence.status is ExactHeadCiStatus.FAILED:
            return self._attempts.mark_failed(
                attempt.attempt_id,
                "provider_ci_failed",
                ci_sha=evidence.sha,
                ci_checks=evidence.checks,
            )
        if evidence.sha != attempt.new_head:
            return self._attempts.mark_failed(attempt.attempt_id, "provider_ci_sha_mismatch")

        current_pr = self._pull_requests.find_by_branch(
            pull_request.repository_slug, pull_request.head_branch
        )
        if current_pr is None or current_pr.run_id != attempt.source_run_id:
            return self._attempts.mark_failed(attempt.attempt_id, "pull_request_identity_changed")
        if current_pr.commit_sha == attempt.previous_head:
            current_pr = self._pull_requests.record_revalidated_revision(
                current_pr, attempt.previous_head, attempt.new_head
            )
        elif current_pr.commit_sha != attempt.new_head:
            return self._attempts.mark_failed(attempt.attempt_id, "persisted_head_changed")

        self._recover_human_gate(task.task_id)
        recovered = self._require_task(task.task_id)
        if recovered.status is not TaskStatus.WAITING_HUMAN:
            raise ValueError("revalidation human gate recovery failed")
        if recovered.blocked_reason is not None:
            recovered.blocked_reason = None
            self._tasks.update(recovered)
        return self._attempts.mark_passed(attempt.attempt_id, evidence.sha, evidence.checks)

    def _recover_human_gate(self, task_id: str) -> None:
        for _ in range(6):
            task = self._require_task(task_id)
            if task.status is TaskStatus.WAITING_HUMAN:
                return
            target = {
                TaskStatus.BLOCKED: TaskStatus.VALIDATING,
                TaskStatus.VALIDATING: TaskStatus.PR_OPEN,
                TaskStatus.PR_OPEN: TaskStatus.WAITING_HUMAN,
            }.get(task.status)
            if target is None:
                raise ValueError("revalidation task left recovery path")
            try:
                self._lifecycle.transition(task_id, target, expected_from=task.status)
            except TaskStateChangedError:
                continue
        raise ValueError("revalidation lifecycle race did not converge")

    def _source(self, task: FactoryTask) -> tuple[AgentRun, PullRequest]:
        for run in reversed(self._runs.list_runs(task.task_id)):
            pr = self._pull_requests.get_for_run(run.run_id)
            if pr is None and run.workspace is not None:
                pr = self._pull_requests.find_by_branch(
                    run.workspace.repository_slug, run.workspace.branch
                )
            if (
                pr is not None
                and pr.task_id == task.task_id
                and pr.repository_slug == task.target_repository
                and pr.run_id == run.run_id
                and pr.number is not None
            ):
                return run, pr
        raise ValueError("revalidation source publication unavailable")

    def _require_attempt_identity(
        self,
        attempt: PostRebaseRevalidation,
        task: FactoryTask,
        run: AgentRun,
        pull_request: PullRequest,
    ) -> None:
        if (
            attempt.task_id != task.task_id
            or attempt.source_run_id != run.run_id
            or attempt.repository_slug != pull_request.repository_slug
            or attempt.head_branch != pull_request.head_branch
            or attempt.pull_request_number != pull_request.number
            or pull_request.commit_sha not in {attempt.previous_head, attempt.new_head}
        ):
            raise ValueError("revalidation identity mismatch")

    @staticmethod
    def _require_revalidation_block(task: FactoryTask) -> None:
        if (
            task.status is not TaskStatus.BLOCKED
            or task.blocked_reason != REVALIDATION_BLOCK_REASON
        ):
            raise ValueError("task is not blocked for post-rebase revalidation")

    def _require_task(self, task_id: str) -> FactoryTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _specs_for(self, task: FactoryTask) -> tuple[QualityGateSpec, ...]:
        if self._registry is not None:
            common = self._registry.resolve(task.project_id, task.target_repository).gates
        else:
            common = self._gate_specs
        ref = task.source.external_ref if task.source is not None else None
        specific = self._task_gate_specs.get(ref, ()) if ref is not None else ()
        return (*common, *specific)

    def _ci_policy(self, task: FactoryTask) -> tuple[bool, tuple[str, ...]]:
        if self._registry is None:
            return True, ()
        profile = self._registry.resolve(task.project_id, task.target_repository)
        return profile.provider_ci_required, profile.required_ci_checks

    def _run_gate(self, spec: QualityGateSpec, workspace: Workspace) -> QualityGate:
        try:
            result = self._gate_runner.run(spec, workspace)
            if result.name != spec.name or result.required != spec.required:
                return QualityGate(
                    spec.name, QualityGateStatus.FAILED, "runner_mismatch", spec.required
                )
            detail = result.detail or ""
            if not re.fullmatch(r"(?:exit_code=-?\d{1,3}|timeout|spawn_error)", detail):
                detail = "redacted"
            return QualityGate(spec.name, result.status, detail, spec.required)
        except FactoryError:
            raise
        except Exception:  # noqa: BLE001
            return QualityGate(spec.name, QualityGateStatus.FAILED, "runner_error", spec.required)


__all__ = [
    "PostRebaseRevalidationService",
    "REVALIDATION_BLOCK_REASON",
    "RevalidationOutcome",
]
