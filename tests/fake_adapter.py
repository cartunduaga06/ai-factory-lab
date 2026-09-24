"""A deterministic in-memory :class:`AgentAdapter` for tests.

This is a real, self-contained adapter implementation, not a mock of the code
under test: it satisfies the protocol structurally and records what it was
asked to do. Dispatch and run-tracking are exercised against it without a
network or a real engine.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from factory.domain.enums import AgentKind, RunStatus
from factory.domain.models import AgentRun, FactoryTask, Workspace


class FakeAgentAdapter:
    """Records dispatch calls and returns a predictable run."""

    def __init__(
        self,
        *,
        kind: AgentKind = AgentKind.OTHER,
        status: RunStatus = RunStatus.PENDING,
        summary: str = "fake run",
        fail_with: Exception | None = None,
        collect_status: RunStatus | None = None,
        collect_summary: str | None = None,
    ) -> None:
        self._kind = kind
        self._status = status
        self._summary = summary
        self._fail_with = fail_with
        self._collect_status = collect_status
        self._collect_summary = collect_summary
        self.dispatched: list[tuple[str, str]] = []
        #: For each dispatch, whether the workspace path physically existed.
        self.path_existed_at_dispatch: list[bool] = []
        #: For each dispatch, the exact path the factory handed over.
        self.received_paths: list[str] = []
        self.collected = 0
        self.cancelled: list[str] = []

    @property
    def kind(self) -> AgentKind:
        return self._kind

    def dispatch(self, task: FactoryTask, workspace: Workspace) -> AgentRun:
        self.dispatched.append((task.task_id, workspace.workspace_id))
        self.received_paths.append(workspace.path)
        self.path_existed_at_dispatch.append(Path(workspace.path).exists())
        if self._fail_with is not None:
            raise self._fail_with
        return AgentRun(
            task_id=task.task_id,
            adapter=self._kind,
            status=self._status,
            workspace=workspace,
            summary=self._summary,
        )

    def collect(self, run: AgentRun) -> AgentRun:
        self.collected += 1
        if self._collect_status is not None:
            run.status = self._collect_status
        if self._collect_summary is not None:
            run.summary = self._collect_summary
        # Mirror the real adapters' contract: a terminal status carries a finish
        # time, a non-terminal one does not.
        if run.status in {RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED}:
            run.finished_at = run.finished_at or datetime.now(UTC)
        else:
            run.finished_at = None
        return run

    def cancel(self, run: AgentRun) -> None:
        self.cancelled.append(run.run_id)


__all__ = ["FakeAgentAdapter"]
