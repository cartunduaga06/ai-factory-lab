"""Human initiated QA rework on an existing, open pull request."""

from __future__ import annotations

import re

from factory.domain.enums import RunStatus, TaskKind, TaskStatus
from factory.domain.models import FactoryTask, PullRequest
from factory.domain.ports import (
    PullRequestRepository,
    PullRequestState,
    PullRequestStateSource,
    RunRepository,
    TaskRepository,
)


class ReworkNotAllowedError(ValueError):
    """The requested review cycle cannot be started safely."""


_SENSITIVE = re.compile(
    r"(?i)(?:github_pat_|gh[pousr]_|sk-[a-z0-9]{16}|"
    r"(?:token|password|secret|authorization)\s*[:=]|bearer\s+\S+)"
)


def sanitize_feedback(raw: str) -> str:
    """Bound QA prose and reject controls and likely credentials before storage."""
    feedback = raw.strip()
    if (
        not feedback
        or len(feedback) > 4000
        or any(not (char.isprintable() or char in "\n\t") for char in feedback)
        or _SENSITIVE.search(feedback)
    ):
        raise ReworkNotAllowedError("QA feedback is empty, unsafe, or exceeds 4000 characters")
    return feedback


class ReworkService:
    """Record one reviewed run's feedback and request a new Codex pass."""

    def __init__(
        self,
        tasks: TaskRepository,
        runs: RunRepository,
        pull_requests: PullRequestRepository,
        state_source: PullRequestStateSource,
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._pull_requests = pull_requests
        self._state_source = state_source

    def request(self, task_id: str, feedback: str) -> FactoryTask:
        clean = sanitize_feedback(feedback)
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status is not TaskStatus.WAITING_HUMAN or task.kind is not TaskKind.CODE:
            raise ReworkNotAllowedError("task is not awaiting code review")
        runs = self._runs.list_runs(task_id)
        if not runs or runs[-1].status is not RunStatus.SUCCEEDED:
            raise ReworkNotAllowedError("reviewed run is missing or unsuccessful")
        run = runs[-1]
        workspace = run.workspace
        pr = self._pull_requests.get_for_run(run.run_id)
        if pr is None and workspace is not None:
            pr = self._pull_requests.find_by_branch(workspace.repository_slug, workspace.branch)
        if (
            workspace is None
            or pr is None
            or not self._matches(task, pr)
            or pr.run_id not in {item.run_id for item in runs}
            or pr.head_branch != workspace.branch
        ):
            raise ReworkNotAllowedError("reviewed pull request identity is stale")
        assert pr is not None
        try:
            state = self._state_source.state(pr)
        except Exception:  # noqa: BLE001 - provider details must not enter the QA record
            raise ReworkNotAllowedError("reviewed pull request state is unavailable") from None
        if state is not PullRequestState.OPEN:
            raise ReworkNotAllowedError("reviewed pull request is no longer open")
        return self._tasks.request_rework(task_id, run.run_id, clean)

    @staticmethod
    def _matches(task: FactoryTask, pr: PullRequest | None) -> bool:
        return bool(
            pr is not None
            and pr.task_id == task.task_id
            and pr.repository_slug == task.target_repository
            and pr.number is not None
            and pr.number > 0
        )
