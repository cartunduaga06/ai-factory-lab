"""Domain-level errors.

Errors that carry meaning about the domain model itself live here so that every
layer can raise or catch them without importing a higher layer. They must never
carry credentials or raw external payloads.
"""

from __future__ import annotations

from factory.domain.enums import TaskStatus
from factory.domain.models import TaskSource


class FactoryError(Exception):
    """Base class for every error the factory raises deliberately."""


class PersistenceError(FactoryError):
    """A persistence operation failed for a reason other than a known conflict."""


class TaskSourceError(FactoryError, ValueError):
    """A task's source identity is missing or malformed for the operation."""


class DuplicateTaskError(FactoryError):
    """A task with the same structured source identity already exists.

    Raised by persistence as a defense-in-depth signal: the uniqueness of
    ``(provider, repository_slug, issue_number)`` is enforced by the storage
    layer, not only by application-level checks.
    """

    def __init__(self, source: TaskSource) -> None:
        super().__init__(
            f"task already exists for source {source.provider}:"
            f"{source.repository_slug}#{source.issue_number}"
        )
        self.source = source


class TaskStateChangedError(FactoryError):
    """A stored task was not in the status a transition expected.

    Signals a lost update: the caller validated ``expected -> target`` against a
    snapshot that no longer matches storage, so the atomic write is refused
    rather than silently applied to a different starting state.
    """

    def __init__(self, task_id: str, expected: TaskStatus, actual: TaskStatus) -> None:
        super().__init__(
            f"task {task_id} status changed: expected {expected.value}, found {actual.value}"
        )
        self.task_id = task_id
        self.expected = expected
        self.actual = actual


class DuplicateRunError(FactoryError):
    """An agent run would duplicate existing run state.

    Raised by persistence when a run with the same ``run_id`` already exists,
    when the task already has an active (non-terminal) run, or when another run
    already owns the workspace the run points at. The unique indexes over active
    runs and over run workspaces are the defense-in-depth guards behind dispatch
    idempotency and the one-workspace-per-run isolation invariant: even if two
    dispatchers slip past the application-level check, storage refuses the
    duplicate.
    """

    def __init__(
        self, run_id: str, task_id: str | None = None, workspace_id: str | None = None
    ) -> None:
        if task_id is not None:
            message = f"task {task_id} already has an active run"
        elif workspace_id is not None:
            message = f"run {run_id} would reuse a workspace owned by another run"
        else:
            message = f"run {run_id} already exists"
        super().__init__(message)
        self.run_id = run_id
        self.task_id = task_id
        self.workspace_id = workspace_id


class DispatchError(FactoryError):
    """Base class for controlled dispatch failures.

    Dispatch never fails with an untyped exception: a task that cannot be
    claimed, a lost race, and an adapter that refuses to start all surface as a
    subclass of this error.
    """


class TaskNotReadyError(DispatchError):
    """A task cannot be dispatched because it is not in ``READY``."""

    def __init__(self, task_id: str, status: TaskStatus) -> None:
        super().__init__(f"task {task_id} is not READY for dispatch (status {status.value})")
        self.task_id = task_id
        self.status = status


class DispatchConflictError(DispatchError):
    """Another dispatcher won the claim race for this task.

    Raised when the atomic ``READY -> CLAIMED`` compare-and-swap finds the task
    already moved on, so this caller must not create a workspace or run.
    """

    def __init__(self, task_id: str) -> None:
        super().__init__(f"task {task_id} was claimed by another dispatcher")
        self.task_id = task_id


class AgentDispatchError(DispatchError):
    """The agent adapter failed while starting a run.

    The failed attempt is still recorded as a durable run, so the error carries
    its ``run_id``. The adapter's own exception is neither embedded in the
    message nor retained as ``__cause__``/``__context__``: it is discarded at the
    adapter boundary, so an engine's error text — which could contain a
    credential — can never leak into factory logs, tracebacks or the CLI.
    """

    def __init__(self, task_id: str, run_id: str) -> None:
        super().__init__(f"adapter failed to start run {run_id} for task {task_id}")
        self.task_id = task_id
        self.run_id = run_id


class AgentCollectError(FactoryError):
    """The agent adapter failed while collecting an active run.

    Run tracking is engine-agnostic, so it cannot trust an adapter to sanitize
    its own errors: a defective or future adapter may raise a raw provider
    exception whose message embeds a credential. The adapter's exception is
    therefore discarded at this boundary and never retained as
    ``__cause__``/``__context__`` — chaining it would let Python render it in the
    traceback, and a token in the message could then reach factory logs, the CLI
    or an exception report. The message is built from factory-domain identifiers
    only (``run_id``, ``task_id``), so this error is safe to log verbatim.

    Nothing is persisted when collection fails: the stored run keeps its previous
    (non-terminal) status and the task stays ``RUNNING``, so a later refresh can
    simply retry collection.
    """

    def __init__(self, run_id: str, task_id: str) -> None:
        super().__init__(f"adapter failed to collect run {run_id} for task {task_id}")
        self.run_id = run_id
        self.task_id = task_id


class WorkspaceProvisioningError(DispatchError):
    """The physical workspace for a run could not be prepared.

    Sanitized like every other dispatch failure: the message is built from
    factory-domain data only (the workspace id), and the underlying git or
    process exception is deliberately not retained as ``__cause__`` or
    ``__context__``. Git writes credentials into stderr for some failures, so
    chaining the raw error would render it in the traceback; discarding it keeps
    command lines, remote URLs and tokens out of logs, exceptions and the CLI.

    The task remains ``CLAIMED``: the READY→CLAIMED claim already committed, and
    the legal recovery is the existing ``BLOCKED``/``CANCELLED`` path, not a
    silent rollback that would rewrite history.
    """

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"workspace {workspace_id} could not be prepared")
        self.workspace_id = workspace_id


class PublicationError(FactoryError):
    """A run's work could not be published.

    The base class for every controlled Phase 5 publication failure. Like the
    dispatch errors, it carries factory-domain identifiers only and never retains
    the underlying git, HTTP or storage exception as ``__cause__`` or
    ``__context__``: those messages can embed a remote URL, a response body or a
    credential, and Python would render them in a traceback.
    """


class TaskNotPublishableError(PublicationError):
    """A task or run does not meet the guard conditions for publication.

    Raised before any commit, push or PR when the run is not ready to publish:
    the task is not ``VALIDATING``, the run does not belong to the task, it is not
    the latest run, it did not succeed, its validation outcome is not
    ``READY_FOR_NEXT_PHASE``, or it has no workspace. A red or incomplete
    validation is never published.
    """

    def __init__(self, task_id: str, run_id: str, reason: str) -> None:
        super().__init__(f"run {run_id} for task {task_id} is not publishable: {reason}")
        self.task_id = task_id
        self.run_id = run_id
        self.reason = reason


class RevisionNotPublishableError(PublicationError):
    """The workspace has nothing the factory may publish.

    Raised when the isolated branch has no publishable diff relative to its base
    and carries no previous factory publication commit, so publishing would
    produce an empty, meaningless commit (or an empty PR). The message names the
    workspace only.
    """

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"workspace {workspace_id} has no publishable changes")
        self.workspace_id = workspace_id


class UnsafeRemoteError(PublicationError):
    """A git remote URL is unsafe to authenticate through and was refused.

    Raised when the remote carries embedded credentials
    (``scheme://user:pass@host``) or uses plaintext ``http://``. The factory never
    authenticates through such a remote: it must not reuse, mutate or persist a
    userinfo-embedded URL, and the write credential must never cross an
    unencrypted transport. Publication stops before any authenticated operation
    runs. The message names the workspace only — never the URL, which is the very
    thing that could contain a secret.
    """

    def __init__(self, workspace_id: str) -> None:
        super().__init__(f"workspace {workspace_id} remote configuration is unsafe")
        self.workspace_id = workspace_id


class DuplicatePullRequestError(FactoryError):
    """A pull request would duplicate existing persisted PR state.

    Raised by persistence when the run already has a stored PR, or when an active
    publication already owns the same target repository and head branch. The
    uniqueness is enforced by storage — the defense-in-depth guard behind
    one-PR-per-run publication idempotency — not only by an application check.
    The message names factory identifiers only.
    """

    def __init__(self, run_id: str, repository_slug: str, head_branch: str) -> None:
        super().__init__(f"run {run_id} already has a persisted pull request")
        self.run_id = run_id
        self.repository_slug = repository_slug
        self.head_branch = head_branch


__all__ = [
    "AgentCollectError",
    "AgentDispatchError",
    "DispatchConflictError",
    "DispatchError",
    "DuplicatePullRequestError",
    "DuplicateRunError",
    "DuplicateTaskError",
    "FactoryError",
    "PersistenceError",
    "PublicationError",
    "RevisionNotPublishableError",
    "TaskNotPublishableError",
    "TaskNotReadyError",
    "TaskSourceError",
    "TaskStateChangedError",
    "UnsafeRemoteError",
    "WorkspaceProvisioningError",
]
