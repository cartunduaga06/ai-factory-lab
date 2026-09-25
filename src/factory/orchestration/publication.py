"""Publish a validated run: commit, push, open a pull request, hand off to a human.

This is the Phase 5 orchestration seam. It depends only on domain ports — the
:class:`~factory.domain.ports.TaskRepository`, ``RunRepository``,
``PullRequestRepository``, ``WorkspacePublisher`` and ``PullRequestSink`` — and
never imports GitHub, OpenHands, SQLite, git or ``subprocess``.

The flow, and where it stops:

```
VALIDATING
   ↓  guard:  task + latest run + SUCCEEDED + READY_FOR_NEXT_PHASE + workspace
   ↓  publish: WorkspacePublisher  (commit, push the isolated branch)
   ↓  provider: find open PR, else open one
   ↓  persist: PullRequestRepository
   ↓  VALIDATING → PR_OPEN → WAITING_HUMAN
STOP   (never merge)
```

Publication is guarded twice over. The *validation* guard refuses a run whose
required gates are not all green, and the *latest-run* guard refuses a superseded
run, so a stale or red attempt can never produce a pull request.

Idempotency and crash recovery are the core of this service. Every step is
independently retry-safe, and a persisted PR short-circuits the whole flow, so a
retry after any crash window:

* A — commit succeeded, crash before push → the existing commit is reused and pushed;
* B — push succeeded, crash before PR → the pushed branch is reused and a PR is opened;
* C — provider PR created, crash before local persistence → the open PR is found by
  branch and persisted, no second PR;
* D — PR persisted, crash before ``VALIDATING → PR_OPEN`` → the lifecycle is reconciled;
* E — task ``PR_OPEN``, crash before ``WAITING_HUMAN`` → reconciled;
* F — task already ``WAITING_HUMAN`` → a no-op returning the same PR.
"""

from __future__ import annotations

from dataclasses import dataclass

from factory.domain.enums import RepositoryRole, RunStatus, TaskStatus, ValidationOutcome
from factory.domain.errors import (
    DuplicatePullRequestError,
    TaskNotPublishableError,
    TaskStateChangedError,
    ValidatedRevisionMissingError,
)
from factory.domain.models import AgentRun, FactoryTask, PullRequest, Repository
from factory.domain.ports import (
    PullRequestRepository,
    PullRequestSink,
    RunRepository,
    TaskRepository,
    WorkspacePublisher,
)
from factory.orchestration.machine import InvalidTransitionError
from factory.orchestration.transitions import TaskLifecycleService

#: Statuses from which publication may be reconciled (or performed).
_PUBLICATION_STATUSES = frozenset(
    {TaskStatus.VALIDATING, TaskStatus.PR_OPEN, TaskStatus.WAITING_HUMAN}
)

#: Branches the factory never publishes from, checked independently of config.
_PROTECTED_BRANCHES = frozenset({"main", "master"})

#: Bound on a deterministic PR title, well under GitHub's 256-char limit.
_MAX_TITLE_LENGTH = 200


@dataclass(slots=True, frozen=True)
class PublicationResult:
    """Outcome of one publication pass."""

    pull_request: PullRequest
    task_status: TaskStatus
    #: Whether this pass created the lifecycle transitions (``False`` = reconciled/no-op).
    opened_now: bool


class PublicationService:
    """Publishes a validated run and stops at ``WAITING_HUMAN``.

    The service takes a task id and run id, verifies the run is publishable,
    commits and pushes the run's isolated branch through the
    :class:`~factory.domain.ports.WorkspacePublisher` port, opens or recovers a
    pull request through the :class:`~factory.domain.ports.PullRequestSink` port,
    persists it, and advances the task ``VALIDATING → PR_OPEN → WAITING_HUMAN``.
    It never merges.
    """

    def __init__(
        self,
        tasks: TaskRepository,
        runs: RunRepository,
        pull_requests: PullRequestRepository,
        *,
        publisher: WorkspacePublisher,
        sink: PullRequestSink,
        base_branch: str = "main",
        default_branch: str = "main",
    ) -> None:
        self._tasks = tasks
        self._runs = runs
        self._pull_requests = pull_requests
        self._publisher = publisher
        self._sink = sink
        self._base_branch = base_branch
        self._default_branch = default_branch
        self._lifecycle = TaskLifecycleService(tasks)

    def publish(self, task_id: str, run_id: str) -> PublicationResult:
        """Publish ``run_id`` of ``task_id`` and return the durable PR result.

        Raises:
            KeyError: if the task or run is unknown.
            TaskNotPublishableError: if the task/run does not meet the guard
                conditions. Nothing is committed, pushed or opened.
            PublicationError: if publication fails in a controlled way. The
                underlying git/HTTP/storage error is discarded, not chained.
        """
        task = self._require(task_id)
        run = self._require_run(run_id, task_id)
        self._require_publishable(task, run)

        existing = self._existing_pull_request(run)
        if existing is not None:
            # A previous pass already published: recover it and reconcile the
            # lifecycle (crash windows C-F). No new commit, push or PR.
            status, opened = self._reconcile(task.task_id)
            return PublicationResult(pull_request=existing, task_status=status, opened_now=opened)

        # From here on we are creating a publication, which is only legal from
        # VALIDATING. A persisted PR short-circuits this, so a retry never needs
        # to re-enter from an advanced status.
        if task.status is not TaskStatus.VALIDATING:
            raise TaskNotPublishableError(
                task.task_id, run.run_id, f"task is {task.status.value}, not VALIDATING"
            )
        if not _validated_revision(run):
            # A green outcome is not enough: the exact workspace revision that
            # passed the gates must have been bound durably at validation time.
            # A run that looks successful but carries no revision identity can
            # never create a new publication. Checked here rather than in the
            # guard so that reconciling an already-published run (crash windows
            # C-F) is never blocked by it.
            raise ValidatedRevisionMissingError(task.task_id, run.run_id)

        revision = self._publisher.publish(task, run)
        pull_request = self._resolve_pull_request(task, run, revision.branch)
        persisted = self._persist(pull_request)
        status, opened = self._reconcile(task.task_id)
        return PublicationResult(pull_request=persisted, task_status=status, opened_now=opened)

    # -- guards ------------------------------------------------------------

    def _require(self, task_id: str) -> FactoryTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _require_run(self, run_id: str, task_id: str) -> AgentRun:
        run = self._runs.get_run(run_id)
        if run is None:
            raise KeyError(run_id)
        if run.task_id != task_id:
            raise TaskNotPublishableError(task_id, run_id, "run does not belong to the task")
        return run

    def _require_publishable(self, task: FactoryTask, run: AgentRun) -> None:
        """Refuse anything that is not a green, latest, isolated run.

        Checked before any write, so a red or incomplete validation can never
        produce a commit, a push or a PR.
        """
        if not self._is_latest_run(run):
            raise TaskNotPublishableError(task.task_id, run.run_id, "run is superseded")
        if run.status is not RunStatus.SUCCEEDED:
            raise TaskNotPublishableError(
                task.task_id, run.run_id, f"run status is {run.status.value}"
            )
        if run.validation_outcome is not ValidationOutcome.READY_FOR_NEXT_PHASE:
            raise TaskNotPublishableError(
                task.task_id, run.run_id, "required quality gates did not pass"
            )
        workspace = run.workspace
        if workspace is None:
            raise TaskNotPublishableError(task.task_id, run.run_id, "run has no workspace")
        if workspace.branch in _PROTECTED_BRANCHES or workspace.branch == self._default_branch:
            raise TaskNotPublishableError(
                task.task_id, run.run_id, "workspace is on a default branch"
            )

    def _is_latest_run(self, run: AgentRun) -> bool:
        """Whether ``run`` is the newest run for its task (durable ordering)."""
        runs = self._runs.list_runs(run.task_id)
        return bool(runs) and runs[-1].run_id == run.run_id

    # -- publication steps -------------------------------------------------

    def _existing_pull_request(self, run: AgentRun) -> PullRequest | None:
        """Return a PR already persisted for this run or its target branch."""
        persisted = self._pull_requests.get_for_run(run.run_id)
        if persisted is not None:
            return persisted
        workspace = run.workspace
        if workspace is None:
            return None
        return self._pull_requests.find_by_branch(workspace.repository_slug, workspace.branch)

    def _resolve_pull_request(
        self, task: FactoryTask, run: AgentRun, head_branch: str
    ) -> PullRequest:
        """Recover an existing open PR for the branch, or open a new one."""
        workspace = run.workspace
        assert workspace is not None  # guarded by _require_publishable
        repository = Repository(slug=workspace.repository_slug, role=RepositoryRole.TARGET)

        found = self._sink.find_open_pull_request(repository, head_branch)
        if found is not None:
            return _with_factory_metadata(found, task, run, self._base_branch)

        requested = PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=head_branch,
            base_branch=self._base_branch,
            title=_bounded_title(task.title),
            body=build_pull_request_body(task, run),
            task_id=task.task_id,
            run_id=run.run_id,
        )
        opened = self._sink.open_pull_request(requested)
        return _with_factory_metadata(opened, task, run, self._base_branch)

    def _persist(self, pull_request: PullRequest) -> PullRequest:
        """Persist the PR idempotently.

        A duplicate is not fatal: it means a previous attempt already stored a PR
        for this run or branch (a lost race), so the stored row is returned rather
        than creating a second one.
        """
        try:
            return self._pull_requests.save(pull_request)
        except DuplicatePullRequestError:
            stored = (
                self._pull_requests.get_for_run(pull_request.run_id)
                if pull_request.run_id is not None
                else None
            )
            if stored is None:
                stored = self._pull_requests.find_by_branch(
                    pull_request.repository_slug, pull_request.head_branch
                )
            if stored is None:
                raise
            return stored

    def _reconcile(self, task_id: str) -> tuple[TaskStatus, bool]:
        """Drive an already-published task to ``WAITING_HUMAN``, idempotently.

        Only the legal publication edges are applied, and only from the
        publication statuses, so reconciliation can never rewind or cross into an
        unrelated state. A task already at ``WAITING_HUMAN`` is left untouched.
        """
        task = self._tasks.get(task_id)
        if task is None:
            raise KeyError(task_id)
        opened = False
        if task.status is TaskStatus.VALIDATING:
            self._apply(task_id, TaskStatus.PR_OPEN)
            opened = True
        task = self._tasks.get(task_id)
        if task is not None and task.status is TaskStatus.PR_OPEN:
            self._apply(task_id, TaskStatus.WAITING_HUMAN)
            opened = True
        current = self._tasks.get(task_id)
        status = current.status if current is not None else TaskStatus.VALIDATING
        return status, opened

    def _apply(self, task_id: str, target: TaskStatus) -> None:
        try:
            self._lifecycle.transition(task_id, target)
        except (InvalidTransitionError, TaskStateChangedError):
            # A concurrent writer already applied this edge; the stored status is
            # the source of truth and reconciliation is complete.
            return


def build_pull_request_body(task: FactoryTask, run: AgentRun) -> str:
    """Build a deterministic, bounded PR body from safe factory metadata only.

    No raw agent output, provider payload, log, environment value or secret is
    ever included. Only the source Issue reference, the task/run identity, the
    validation outcome and the gate names/statuses are recorded.
    """
    lines = [
        "Automated publication by AI Factory Lab.",
        "",
        "This pull request awaits a mandatory human review. The factory never merges.",
        "",
        f"- task id: `{task.task_id}`",
        f"- run id: `{run.run_id}`",
    ]
    if task.source is not None:
        lines.append(f"- source issue: `{task.source.external_ref}`")
    lines.append(f"- validation: `{run.validation_outcome.value}`")
    lines.append("")
    if run.gates:
        lines.append("Quality gates:")
        for gate in run.gates:
            requirement = "required" if gate.required else "optional"
            lines.append(f"- `{gate.name}`: {gate.status.value} ({requirement})")
    else:
        lines.append("Quality gates: none configured.")
    return "\n".join(lines)


def _validated_revision(run: AgentRun) -> str:
    """The run's bound validated revision, or an empty string if it has none.

    A green validation must have bound the exact workspace revision that passed
    the gates. Whitespace-only values are treated as absent.
    """
    revision = run.validated_revision
    return revision.strip() if revision else ""


def _bounded_title(title: str) -> str:
    """Return a bounded, non-printable-free PR title derived from the task title."""
    cleaned = "".join(ch for ch in title if ch.isprintable()).strip()
    if not cleaned:
        return "factory change"
    return cleaned[:_MAX_TITLE_LENGTH]


def _with_factory_metadata(
    pull_request: PullRequest, task: FactoryTask, run: AgentRun, base_branch: str
) -> PullRequest:
    """Return ``pull_request`` with factory-owned identity and a bounded title/body."""
    return PullRequest(
        repository_slug=pull_request.repository_slug,
        head_branch=pull_request.head_branch,
        base_branch=base_branch,
        title=_bounded_title(pull_request.title or task.title),
        body=build_pull_request_body(task, run),
        number=pull_request.number,
        url=pull_request.url,
        task_id=task.task_id,
        run_id=run.run_id,
        opened_at=pull_request.opened_at,
        merged=pull_request.merged,
    )


__all__ = ["PublicationResult", "PublicationService", "build_pull_request_body"]
