"""Explicit, evidence-bound operator authorization for terminal Codex timeouts.

This never dispatches, retries, resumes a Sprint, or adopts an existing PR.
Normal FAILED transitions remain forbidden. A subsequent explicit retry
may resume the exact old workspace, after the normal human Sprint gate.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from factory.domain.enums import AgentKind, RepositoryRole, RunStatus, TaskKind, TaskStatus
from factory.domain.models import FactoryTask, Repository
from factory.domain.ports import (
    PullRequestRepository,
    PullRequestSink,
    RunRepository,
    WorkspaceProvisioner,
)
from factory.orchestration.recovery import RecoveryPolicy
from factory.orchestration.sprint import SprintService


class TerminalRecoveryTasks(Protocol):
    """Narrow domain port: only the exceptional, atomic terminal action."""

    def get(self, task_id: str) -> FactoryTask | None: ...

    def authorize_terminal_timeout_recovery(self, task_id: str, run_id: str) -> FactoryTask: ...


class TerminalRecoveryRefused(ValueError):
    """Recovery was not fully evidenced or authorized; no state was changed."""


class TerminalRecoveryService:
    """Authorize one failed timeout without erasing its run or existing code."""

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
        sprint: SprintService | None = None,
        policy: RecoveryPolicy | None = None,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._prs = prs
        self._sink = sink
        self._provisioner = provisioner
        self._root = workspace_root
        self._base = base_branch
        self._eligible = source_is_eligible
        self._sprint = sprint
        self._policy = policy or RecoveryPolicy()

    def authorize(
        self, task_id: str, expected_run_id: str, *, acknowledge_timeout: bool
    ) -> FactoryTask:
        """Move only a verified CODE timeout FAILED -> BLOCKED (no dispatch)."""
        if not acknowledge_timeout:
            raise TerminalRecoveryRefused("explicit timeout acknowledgement is required")
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
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
            or workspace.branch == self._base
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
        if self._sprint is not None and not self._sprint.allows_review(task):
            raise TerminalRecoveryRefused("task is outside current authorized Sprint")
        if task.source is None or not self._eligible(task):
            raise TerminalRecoveryRefused("source Issue is not currently eligible")
        if self._prs.find_by_branch(task.target_repository, workspace.branch) is not None:
            raise TerminalRecoveryRefused("existing local PR requires human review")
        # Fail closed on lookup errors. A manually recovered PR like E4 #68 must
        # never be overwritten by an automatic retry on its published branch.
        remote = self._sink.find_open_pull_request(
            Repository(task.target_repository, role=RepositoryRole.TARGET),
            workspace.branch,
            self._base,
        )
        if remote is not None:
            raise TerminalRecoveryRefused("existing provider PR requires human review")
        # Verify checkout/branch without resetting or deleting partial edits.
        self._provisioner.repair(task, workspace)
        return self._tasks.authorize_terminal_timeout_recovery(task_id, expected_run_id)
