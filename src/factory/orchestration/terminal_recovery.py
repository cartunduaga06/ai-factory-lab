"""Explicit, evidence-bound operator authorization for terminal Codex failures.

This never dispatches, retries, resumes a Sprint, or adopts an existing PR.
Normal FAILED transitions remain forbidden. Timeout recovery preserves the
existing workspace; clean non-timeout worker recovery still requires a later
explicit retry before any new dispatch.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from factory.domain.enums import AgentKind, RepositoryRole, RunStatus, TaskKind, TaskStatus
from factory.domain.models import FactoryTask, Repository, Workspace
from factory.domain.ports import (
    PullRequestRepository,
    PullRequestSink,
    RunRepository,
    WorkspaceProvisioner,
)
from factory.domain.projects import ProjectRegistry, ProjectRoutingError
from factory.orchestration.recovery import RecoveryPolicy
from factory.orchestration.sprint import SprintService


class TerminalRecoveryTasks(Protocol):
    """Narrow domain port: only the exceptional, atomic terminal action."""

    def get(self, task_id: str) -> FactoryTask | None: ...

    def authorize_terminal_timeout_recovery(self, task_id: str, run_id: str) -> FactoryTask: ...
    def authorize_terminal_worker_recovery(self, task_id: str, run_id: str) -> FactoryTask: ...
    def authorize_orphaned_codex_recovery(self, task_id: str, run_id: str) -> FactoryTask: ...


class TerminalRecoveryRefused(ValueError):
    """Recovery was not fully evidenced or authorized; no state was changed."""


class TerminalRecoveryService:
    """Authorize narrowly evidenced terminal Codex recovery without erasing history."""

    def __init__(
        self,
        tasks: TerminalRecoveryTasks,
        runs: RunRepository,
        prs: PullRequestRepository,
        sink: PullRequestSink,
        provisioner: WorkspaceProvisioner,
        *,
        workspace_root: str,
        base_branch: str,
        source_is_eligible: Callable[[FactoryTask], bool],
        workspace_is_clean: Callable[[Workspace, str], bool] | None = None,
        sprint: SprintService | None = None,
        policy: RecoveryPolicy | None = None,
        registry: ProjectRegistry | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._prs = prs
        self._sink = sink
        self._provisioner = provisioner
        self._root = workspace_root
        self._base = base_branch
        self._eligible = source_is_eligible
        self._workspace_is_clean = workspace_is_clean
        self._sprint = sprint
        self._policy = policy or RecoveryPolicy()
        self._registry = registry

    def authorize(
        self, task_id: str, expected_run_id: str, *, acknowledge_timeout: bool
    ) -> FactoryTask:
        """Move only a verified CODE timeout FAILED -> BLOCKED (no dispatch)."""
        if not acknowledge_timeout:
            raise TerminalRecoveryRefused("explicit timeout acknowledgement is required")
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        try:
            base = (
                self._registry.resolve(task.project_id, task.target_repository).base_ref
                if self._registry is not None
                else self._base
            )
        except ProjectRoutingError:
            raise TerminalRecoveryRefused("project identity mismatch") from None
        if task.status is not TaskStatus.FAILED or task.kind is not TaskKind.CODE:
            raise TerminalRecoveryRefused("task must be terminal FAILED CODE")
        history = self._runs.list_runs(task_id)
        if not history or history[-1].run_id != expected_run_id:
            raise TerminalRecoveryRefused("expected run is not the latest attempt")
        if (
            len([r for r in history if r.status is RunStatus.FAILED])
            >= self._policy.run_retry_limit
        ):
            raise TerminalRecoveryRefused("bounded failure limit reached")
        last = history[-1]
        workspace = last.workspace
        if (
            last.status is not RunStatus.FAILED
            or last.project_id != task.project_id
            or last.adapter is not AgentKind.CODEX
            or workspace is None
            or workspace.kind is not TaskKind.CODE
            or workspace.repository_slug != task.target_repository
        ):
            raise TerminalRecoveryRefused("latest failed CODEX run/workspace required")
        path = Path(workspace.path).expanduser()
        root = Path(self._root).expanduser().resolve(strict=True)
        if (
            path.is_symlink()
            or not path.is_dir()
            or path.resolve(strict=True).parent != root
            or workspace.branch != f"factory/{task.task_id}/{workspace.workspace_id}"
            or workspace.branch == base
        ):
            raise TerminalRecoveryRefused("factory workspace identity is not valid")
        evidence = root / ".factory-codex-runs" / (expected_run_id + ".result")
        if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_uid != os.getuid():
            raise TerminalRecoveryRefused("trusted worker result evidence is missing")
        try:
            if evidence.stat().st_size > 512:
                raise ValueError("oversized evidence")
            result = json.loads(evidence.read_text(encoding="ascii"))
            if not isinstance(result, dict):
                raise ValueError("invalid evidence")
        except (OSError, UnicodeError, ValueError):
            raise TerminalRecoveryRefused("invalid worker result evidence") from None
        if result.get("status") != "FAILED" or type(result.get("exit_code")) is not int:
            raise TerminalRecoveryRefused("worker did not record a failed process")
        if result["exit_code"] != -9 or result.get("timed_out") is not True:
            raise TerminalRecoveryRefused("trusted worker deadline evidence is missing")
        if self._runs.find_active_run(task_id) is not None:
            raise TerminalRecoveryRefused("task has an active run")
        if self._sprint is None or not self._sprint.allows_review(task):
            raise TerminalRecoveryRefused("an authorized Sprint is required for recovery")
        if task.source is None or not self._eligible(task):
            raise TerminalRecoveryRefused("source Issue is not currently eligible")
        if self._prs.find_by_branch(task.target_repository, workspace.branch) is not None:
            raise TerminalRecoveryRefused("existing local PR requires human review")
        # Fail closed on lookup errors. A manually recovered PR like E4 #68 must
        # never be overwritten by an automatic retry on its published branch.
        remote = self._sink.find_open_pull_request(
            Repository(task.target_repository, role=RepositoryRole.TARGET),
            workspace.branch,
            base,
        )
        if remote is not None:
            raise TerminalRecoveryRefused("existing provider PR requires human review")
        # Verify checkout/branch without resetting or deleting partial edits.
        self._provisioner.repair(task, workspace)
        return self._tasks.authorize_terminal_timeout_recovery(task_id, expected_run_id)

    def authorize_worker_failure(
        self, task_id: str, expected_run_id: str, *, acknowledge_failure: bool
    ) -> FactoryTask:
        """Recover one clean, unpublished non-timeout Codex worker exit."""
        if not acknowledge_failure:
            raise TerminalRecoveryRefused("explicit worker failure acknowledgement is required")
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        try:
            base = (
                self._registry.resolve(task.project_id, task.target_repository).base_ref
                if self._registry is not None
                else self._base
            )
        except ProjectRoutingError:
            raise TerminalRecoveryRefused("project identity mismatch") from None
        if task.status is not TaskStatus.FAILED or task.kind is not TaskKind.CODE:
            raise TerminalRecoveryRefused("task must be terminal FAILED CODE")
        history = self._runs.list_runs(task_id)
        if not history or history[-1].run_id != expected_run_id:
            raise TerminalRecoveryRefused("expected run is not the latest attempt")
        if (
            len([r for r in history if r.status is RunStatus.FAILED])
            >= self._policy.run_retry_limit
        ):
            raise TerminalRecoveryRefused("bounded failure limit reached")
        last = history[-1]
        workspace = last.workspace
        if (
            last.status is not RunStatus.FAILED
            or last.project_id != task.project_id
            or last.adapter is not AgentKind.CODEX
            or workspace is None
            or workspace.kind is not TaskKind.CODE
            or workspace.repository_slug != task.target_repository
        ):
            raise TerminalRecoveryRefused("latest failed CODEX run/workspace required")
        path = Path(workspace.path).expanduser()
        root = Path(self._root).expanduser().resolve(strict=True)
        if (
            path.is_symlink()
            or not path.is_dir()
            or path.resolve(strict=True).parent != root
            or workspace.branch != f"factory/{task.task_id}/{workspace.workspace_id}"
            or workspace.branch == base
        ):
            raise TerminalRecoveryRefused("factory workspace identity is not valid")
        evidence = root / ".factory-codex-runs" / (expected_run_id + ".result")
        if evidence.is_symlink() or not evidence.is_file() or evidence.stat().st_uid != os.getuid():
            raise TerminalRecoveryRefused("trusted worker result evidence is missing")
        try:
            if evidence.stat().st_size > 512:
                raise ValueError("oversized evidence")
            result = json.loads(evidence.read_text(encoding="ascii"))
            if not isinstance(result, dict):
                raise ValueError("invalid evidence")
        except (OSError, UnicodeError, ValueError):
            raise TerminalRecoveryRefused("invalid worker result evidence") from None
        if (
            result.get("status") != "FAILED"
            or type(result.get("exit_code")) is not int
            or result["exit_code"] == 0
            or result.get("timed_out") is not False
        ):
            raise TerminalRecoveryRefused("trusted clean worker failure evidence is missing")
        if self._runs.find_active_run(task_id) is not None:
            raise TerminalRecoveryRefused("task has an active run")
        if task.source is None or not self._eligible(task):
            raise TerminalRecoveryRefused("source Issue is not currently eligible")
        if self._prs.find_by_branch(task.target_repository, workspace.branch) is not None:
            raise TerminalRecoveryRefused("existing local PR requires human review")
        remote = self._sink.find_open_pull_request(
            Repository(task.target_repository, role=RepositoryRole.TARGET),
            workspace.branch,
            base,
        )
        if remote is not None:
            raise TerminalRecoveryRefused("existing provider PR requires human review")
        self._provisioner.repair(task, workspace)
        if self._workspace_is_clean is None:
            raise TerminalRecoveryRefused("workspace cleanliness verifier is unavailable")
        try:
            clean = self._workspace_is_clean(workspace, base)
        except Exception:  # noqa: BLE001 - verifier details never cross this boundary
            clean = False
        if not clean:
            raise TerminalRecoveryRefused("workspace contains unpublished changes")
        return self._tasks.authorize_terminal_worker_recovery(task_id, expected_run_id)

    def authorize_orphaned_codex(
        self, task_id: str, expected_run_id: str, *, acknowledge_orphan: bool
    ) -> FactoryTask:
        """Recover a missing-worker RUNNING Codex attempt after independent checks."""
        if not acknowledge_orphan:
            raise TerminalRecoveryRefused("explicit orphan acknowledgement is required")
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        try:
            base = (
                self._registry.resolve(task.project_id, task.target_repository).base_ref
                if self._registry
                else self._base
            )
        except ProjectRoutingError:
            raise TerminalRecoveryRefused("project identity mismatch") from None
        history = self._runs.list_runs(task_id)
        if task.status is not TaskStatus.RUNNING or task.kind is not TaskKind.CODE:
            raise TerminalRecoveryRefused("task must be RUNNING CODE")
        if not history or history[-1].run_id != expected_run_id:
            raise TerminalRecoveryRefused("expected run is not the latest attempt")
        run = history[-1]
        workspace = run.workspace
        if (
            run.status is not RunStatus.RUNNING
            or run.adapter is not AgentKind.CODEX
            or run.project_id != task.project_id
            or workspace is None
            or workspace.kind is not TaskKind.CODE
            or workspace.repository_slug != task.target_repository
        ):
            raise TerminalRecoveryRefused("latest RUNNING CODEX run/workspace required")
        root = Path(self._root).expanduser().resolve(strict=True)
        path = Path(workspace.path).expanduser()
        if (
            path.is_symlink()
            or not path.is_dir()
            or path.resolve(strict=True).parent != root
            or workspace.branch != f"factory/{task.task_id}/{workspace.workspace_id}"
            or workspace.branch == base
        ):
            raise TerminalRecoveryRefused("factory workspace identity is not valid")
        state = root / ".factory-codex-runs"
        result = state / f"{expected_run_id}.result"
        liveness = state / f"{expected_run_id}.alive"
        if (
            liveness.is_symlink()
            or not liveness.is_file()
            or liveness.stat().st_uid != os.getuid()
            or liveness.stat().st_size > 512
        ):
            raise TerminalRecoveryRefused("trusted worker liveness evidence is missing")
        try:
            evidence = json.loads(liveness.read_text(encoding="ascii"))
            if not isinstance(evidence, dict):
                raise ValueError
            raw_pids = [evidence.get("worker_pid"), evidence.get("codex_pid")]
            if any(type(pid) is not int or pid <= 0 for pid in raw_pids):
                raise ValueError
            pids = [pid for pid in raw_pids if type(pid) is int]
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            raise TerminalRecoveryRefused("worker liveness evidence is ambiguous") from None
        for pid in pids:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                continue
            except PermissionError:
                raise TerminalRecoveryRefused("worker liveness is ambiguous") from None
            else:
                raise TerminalRecoveryRefused("Codex worker is still alive")
        if result.exists() or result.is_symlink():
            raise TerminalRecoveryRefused("terminal worker result exists or is ambiguous")
        if self._runs.find_active_run(task_id) is None:
            raise TerminalRecoveryRefused("active Codex run is missing")
        if self._runs.find_active_run(task_id) != run:
            raise TerminalRecoveryRefused("active run identity is ambiguous")
        if (
            self._sprint is None
            or not self._sprint.allows_review(task)
            or task.source is None
            or not self._eligible(task)
        ):
            raise TerminalRecoveryRefused("source Issue or authorized Sprint is not eligible")
        if self._prs.find_by_branch(task.target_repository, workspace.branch) is not None:
            raise TerminalRecoveryRefused("existing local PR requires human review")
        if (
            self._sink.find_open_pull_request(
                Repository(task.target_repository, RepositoryRole.TARGET), workspace.branch, base
            )
            is not None
        ):
            raise TerminalRecoveryRefused("existing provider PR requires human review")
        if self._workspace_is_clean is None:
            raise TerminalRecoveryRefused("workspace cleanliness verifier is unavailable")
        try:
            if not self._workspace_is_clean(workspace, base):
                raise TerminalRecoveryRefused("workspace contains unpublished changes")
            self._provisioner.repair(task, workspace)
        except TerminalRecoveryRefused:
            raise
        except Exception:
            raise TerminalRecoveryRefused("workspace evidence is ambiguous") from None
        return self._tasks.authorize_orphaned_codex_recovery(task_id, expected_run_id)
