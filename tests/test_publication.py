"""Tests for the publication orchestration service.

The service is exercised with real SQLite repositories and real (non-mock) port
doubles from :mod:`tests.fake_publish`. No git, no network, no engine.
"""

from __future__ import annotations

import traceback
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus, TaskStatus
from factory.domain.errors import (
    PublicationError,
    PullRequestIdentityError,
    TaskNotPublishableError,
    ValidatedRevisionMissingError,
)
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PullRequest,
    QualityGate,
    TaskSource,
    Workspace,
)
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.orchestration import PublicationService
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    return str(tmp_path / "factory.db")


def _tasks(db_path: str) -> SqliteTaskRepository:
    repo = SqliteTaskRepository(db_path)
    repo.initialize()
    return repo


def _runs(db_path: str) -> SqliteRunRepository:
    repo = SqliteRunRepository(db_path)
    repo.initialize()
    return repo


def _prs(db_path: str) -> SqlitePullRequestRepository:
    repo = SqlitePullRequestRepository(db_path)
    repo.initialize()
    return repo


def _task(tasks: SqliteTaskRepository, *, title: str = "Add widget") -> FactoryTask:
    task = tasks.save(
        FactoryTask(
            title=title,
            target_repository="example/target",
            source=TaskSource("github", "example/control", 7),
        )
    )
    for source, target in (
        (TaskStatus.DISCOVERED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
        (TaskStatus.CLAIMED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.VALIDATING),
    ):
        tasks.apply_transition(task.task_id, source, target)
    task.status = TaskStatus.VALIDATING
    return task


def _validated_run(
    runs: SqliteRunRepository,
    task: FactoryTask,
    tmp_path: Path,
    *,
    run_id: str = "run-1",
    gate_status: QualityGateStatus = QualityGateStatus.PASSED,
    status: RunStatus = RunStatus.SUCCEEDED,
    validated_revision: str | None = "tree-validated-1",
) -> AgentRun:
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch=f"factory/{task.task_id}/ws-{run_id}",
        path=str(tmp_path / run_id),
    )
    Path(workspace.path).mkdir(parents=True, exist_ok=True)
    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.OTHER,
        run_id=run_id,
        status=status,
        workspace=workspace,
        gates=(QualityGate(name="tests", status=gate_status, required=True),),
        validated_revision=validated_revision,
    )
    runs.save_run(run)
    return run


def _service(
    db_path: str,
    *,
    publisher: FakeWorkspacePublisher | None = None,
    sink: FakePullRequestSink | None = None,
) -> PublicationService:
    return PublicationService(
        _tasks(db_path),
        _runs(db_path),
        _prs(db_path),
        publisher=publisher or FakeWorkspacePublisher(),
        sink=sink or FakePullRequestSink(),
        base_branch="main",
        default_branch="main",
    )


# -- happy path ------------------------------------------------------------


def test_publication_commits_pushes_opens_pr_and_reaches_waiting_human(
    db_path: str, tmp_path: Path
) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()

    result = _service(db_path, publisher=publisher, sink=sink).publish(task.task_id, run.run_id)

    assert publisher.calls == 1
    assert publisher.published == [(task.task_id, run.workspace.branch)]  # type: ignore[union-attr]
    assert sink.create_calls == 1
    assert result.pull_request.number is not None
    assert result.pull_request.run_id == run.run_id
    assert result.pull_request.task_id == task.task_id
    assert result.task_status is TaskStatus.WAITING_HUMAN
    assert result.opened_now is True
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN
    # Exactly one PR persisted, associated with the run and task.
    stored = _prs(db_path).get_for_run(run.run_id)
    assert stored is not None and stored.number == result.pull_request.number


def test_lifecycle_passes_through_pr_open_to_waiting_human(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)

    _service(db_path).publish(task.task_id, run.run_id)

    history = [t.to_status for t in tasks.history(task.task_id)]
    assert history[-2:] == [TaskStatus.PR_OPEN, TaskStatus.WAITING_HUMAN]


def test_pr_body_contains_only_factory_metadata(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)

    result = _service(db_path).publish(task.task_id, run.run_id)

    body = result.pull_request.body
    assert task.task_id in body
    assert run.run_id in body
    assert "example/control#7" in body
    assert "tests" in body
    assert "READY_FOR_NEXT_PHASE" in body


# -- guard: validation -----------------------------------------------------


def test_failed_required_gate_blocks_publication(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path, gate_status=QualityGateStatus.FAILED)
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()

    with pytest.raises(TaskNotPublishableError):
        _service(db_path, publisher=publisher, sink=sink).publish(task.task_id, run.run_id)

    assert publisher.calls == 0
    assert sink.create_calls == 0
    assert sink.find_calls == 0
    assert _prs(db_path).get_for_run(run.run_id) is None
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


@pytest.mark.parametrize("status", [QualityGateStatus.PENDING, QualityGateStatus.SKIPPED])
def test_incomplete_required_gate_blocks_publication(
    db_path: str, tmp_path: Path, status: QualityGateStatus
) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path, gate_status=status)
    publisher = FakeWorkspacePublisher()

    with pytest.raises(TaskNotPublishableError):
        _service(db_path, publisher=publisher).publish(task.task_id, run.run_id)

    assert publisher.calls == 0
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


@pytest.mark.parametrize("status", [RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED])
def test_non_succeeded_run_blocks_publication(
    db_path: str, tmp_path: Path, status: RunStatus
) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path, status=status)
    publisher = FakeWorkspacePublisher()

    with pytest.raises(TaskNotPublishableError):
        _service(db_path, publisher=publisher).publish(task.task_id, run.run_id)

    assert publisher.calls == 0


def test_run_without_workspace_blocks_publication(db_path: str) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    runs = _runs(db_path)
    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.OTHER,
        run_id="run-1",
        status=RunStatus.SUCCEEDED,
        gates=(QualityGate(name="tests", status=QualityGateStatus.PASSED, required=True),),
    )
    runs.save_run(run)
    publisher = FakeWorkspacePublisher()

    with pytest.raises(TaskNotPublishableError):
        _service(db_path, publisher=publisher).publish(task.task_id, run.run_id)

    assert publisher.calls == 0


# -- guard: latest run -----------------------------------------------------


def test_superseded_run_cannot_publish(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    runs = _runs(db_path)
    old = _validated_run(runs, task, tmp_path, run_id="run-old")
    # A newer run exists for the task (durable ordering: list_runs is oldest-first).
    _validated_run(runs, task, tmp_path, run_id="run-new")
    publisher = FakeWorkspacePublisher()

    with pytest.raises(TaskNotPublishableError):
        _service(db_path, publisher=publisher).publish(task.task_id, old.run_id)

    assert publisher.calls == 0


# -- idempotency / crash recovery ------------------------------------------


def test_repeated_publication_is_a_no_op(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()
    service = _service(db_path, publisher=publisher, sink=sink)

    first = service.publish(task.task_id, run.run_id)
    second = service.publish(task.task_id, run.run_id)

    assert second.pull_request.number == first.pull_request.number
    assert second.pull_request.head_branch == first.pull_request.head_branch
    assert second.opened_now is False
    # No second commit, push or PR.
    assert publisher.calls == 1
    assert sink.create_calls == 1
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN
    waiting = [t for t in tasks.history(task.task_id) if t.to_status is TaskStatus.WAITING_HUMAN]
    assert len(waiting) == 1


def test_crash_after_push_before_pr_recovers_existing_provider_pr(
    db_path: str, tmp_path: Path
) -> None:
    """Crash window C: the provider has the PR but the factory never persisted it."""
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None
    sink = FakePullRequestSink()
    sink.seed(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="main",
            title="existing",
            number=42,
            url="https://example.invalid/42",
        )
    )
    publisher = FakeWorkspacePublisher()

    result = _service(db_path, publisher=publisher, sink=sink).publish(task.task_id, run.run_id)

    assert result.pull_request.number == 42
    assert sink.create_calls == 0
    assert _prs(db_path).get_for_run(run.run_id) is not None
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN


def test_crash_after_persistence_before_lifecycle_is_reconciled(
    db_path: str, tmp_path: Path
) -> None:
    """Crash window D: PR persisted, task still VALIDATING."""
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None
    _prs(db_path).save(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="main",
            title="persisted",
            number=7,
            url="https://example.invalid/7",
            task_id=task.task_id,
            run_id=run.run_id,
        )
    )
    publisher = FakeWorkspacePublisher()

    result = _service(db_path, publisher=publisher).publish(task.task_id, run.run_id)

    assert result.pull_request.number == 7
    assert publisher.calls == 0
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN


def test_crash_at_pr_open_is_reconciled_to_waiting_human(db_path: str, tmp_path: Path) -> None:
    """Crash window E: task PR_OPEN with a persisted PR."""
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    tasks.apply_transition(task.task_id, TaskStatus.VALIDATING, TaskStatus.PR_OPEN)
    workspace = run.workspace
    assert workspace is not None
    _prs(db_path).save(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="main",
            title="persisted",
            number=8,
            run_id=run.run_id,
        )
    )

    result = _service(db_path).publish(task.task_id, run.run_id)

    assert result.pull_request.number == 8
    assert result.task_status is TaskStatus.WAITING_HUMAN
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN


def test_task_already_waiting_human_is_a_no_op(db_path: str, tmp_path: Path) -> None:
    """Crash window F: task already WAITING_HUMAN."""
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    tasks.apply_transition(task.task_id, TaskStatus.VALIDATING, TaskStatus.PR_OPEN)
    tasks.apply_transition(task.task_id, TaskStatus.PR_OPEN, TaskStatus.WAITING_HUMAN)
    workspace = run.workspace
    assert workspace is not None
    _prs(db_path).save(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="main",
            title="persisted",
            number=9,
            run_id=run.run_id,
        )
    )
    publisher = FakeWorkspacePublisher()

    result = _service(db_path, publisher=publisher).publish(task.task_id, run.run_id)

    assert result.pull_request.number == 9
    assert result.opened_now is False
    assert publisher.calls == 0
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN
    # 4 transitions reach VALIDATING; publication adds PR_OPEN and WAITING_HUMAN.
    assert len(tasks.history(task.task_id)) == 6


def test_retry_reuses_same_commit_and_branch(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    publisher = FakeWorkspacePublisher()
    service = _service(db_path, publisher=publisher)

    first = service.publish(task.task_id, run.run_id)
    second = service.publish(task.task_id, run.run_id)

    assert second.pull_request.head_branch == first.pull_request.head_branch
    assert publisher.calls == 1


def test_provider_failure_does_not_persist_or_advance(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    sink = FakePullRequestSink(fail_create=True)

    with pytest.raises(PublicationError):
        _service(db_path, sink=sink).publish(task.task_id, run.run_id)

    assert _prs(db_path).get_for_run(run.run_id) is None
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_create_failure_with_only_wrong_base_pr_does_not_persist(
    db_path: str, tmp_path: Path
) -> None:
    """REGRESSION 5: a create failure must not adopt a wrong-base provider PR."""
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None
    sink = FakePullRequestSink(fail_create=True)
    sink.seed(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="release",  # same head, wrong base
            title="wrong base",
            number=55,
            url="https://example.invalid/55",
        )
    )
    publisher = FakeWorkspacePublisher()

    with pytest.raises(PublicationError):
        _service(db_path, publisher=publisher, sink=sink).publish(task.task_id, run.run_id)

    assert _prs(db_path).get_for_run(run.run_id) is None
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_wrong_base_provider_pr_is_not_adopted_and_a_correct_one_is_opened(
    db_path: str, tmp_path: Path
) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None
    sink = FakePullRequestSink()
    sink.seed(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="release",
            title="wrong base",
            number=56,
            url="https://example.invalid/56",
        )
    )

    result = _service(db_path, sink=sink).publish(task.task_id, run.run_id)

    # The wrong-base PR was ignored; a new correct PR was created and persisted.
    assert result.pull_request.number != 56
    assert result.pull_request.base_branch == "main"
    assert sink.create_calls == 1
    assert result.task_status is TaskStatus.WAITING_HUMAN


def test_inconsistent_create_response_does_not_persist_or_advance(
    db_path: str, tmp_path: Path
) -> None:
    """A create response with the wrong base is refused, not trusted."""
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink(
        corrupt_create=PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="release",  # provider confirms the wrong base
            title="inconsistent",
            number=60,
            url="https://example.invalid/60",
        )
    )

    with pytest.raises(PublicationError):
        _service(db_path, publisher=publisher, sink=sink).publish(task.task_id, run.run_id)

    assert _prs(db_path).get_for_run(run.run_id) is None
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_exact_create_response_is_persisted_and_advances(db_path: str, tmp_path: Path) -> None:
    """An exact create response is accepted directly and drives the lifecycle."""
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None
    sink = FakePullRequestSink(
        corrupt_create=PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="main",
            title="exact",
            number=61,
            url="https://example.invalid/61",
        )
    )

    result = _service(db_path, sink=sink).publish(task.task_id, run.run_id)

    assert result.pull_request.number == 61
    assert result.pull_request.base_branch == "main"
    assert result.task_status is TaskStatus.WAITING_HUMAN
    stored = _prs(db_path).get_for_run(run.run_id)
    assert stored is not None and stored.number == 61


def test_unknown_task_or_run_raises_key_error(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    service = _service(db_path)

    with pytest.raises(KeyError):
        service.publish("nope", run.run_id)
    with pytest.raises(KeyError):
        service.publish(task.task_id, "nope")


def test_run_for_another_task_is_refused(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    other = tasks.save(FactoryTask(title="Other", target_repository="example/target"))

    with pytest.raises(TaskNotPublishableError):
        _service(db_path).publish(other.task_id, run.run_id)


# -- guard: validated revision ---------------------------------------------


def test_missing_validated_revision_blocks_publication(db_path: str, tmp_path: Path) -> None:
    # SUCCEEDED with green gate objects but no bound revision: not publishable.
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path, validated_revision=None)
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()

    with pytest.raises(ValidatedRevisionMissingError):
        _service(db_path, publisher=publisher, sink=sink).publish(task.task_id, run.run_id)

    assert publisher.calls == 0
    assert sink.find_calls == 0
    assert sink.create_calls == 0
    assert _prs(db_path).get_for_run(run.run_id) is None
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_missing_validated_revision_does_not_affect_recovery(db_path: str, tmp_path: Path) -> None:
    # A persisted PR short-circuits the guard: a previous publication already
    # happened, so reconciliation proceeds even without a bound revision.
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path, validated_revision=None)
    workspace = run.workspace
    assert workspace is not None
    _prs(db_path).save(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="main",
            title="persisted",
            number=11,
            run_id=run.run_id,
        )
    )
    publisher = FakeWorkspacePublisher()

    result = _service(db_path, publisher=publisher).publish(task.task_id, run.run_id)

    assert result.pull_request.number == 11
    assert publisher.calls == 0
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN


# -- persisted PR identity -------------------------------------------------


def test_persisted_pr_with_wrong_base_is_refused(db_path: str, tmp_path: Path) -> None:
    """REGRESSION 6: a stored wrong-base row must not be reconciled or overwritten."""
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None
    _prs(db_path).save(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="release",  # factory expects "main"
            title="persisted",
            number=12,
            run_id=run.run_id,
        )
    )
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()

    with pytest.raises(PullRequestIdentityError):
        _service(db_path, publisher=publisher, sink=sink).publish(task.task_id, run.run_id)

    # No lifecycle advancement, no new commit/push/PR, and the row is untouched.
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    assert publisher.calls == 0
    assert sink.create_calls == 0
    stored = _prs(db_path).get_for_run(run.run_id)
    assert stored is not None
    assert stored.base_branch == "release"


def test_persisted_pr_with_wrong_repository_is_refused(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None
    _prs(db_path).save(
        PullRequest(
            repository_slug="example/other-repo",
            head_branch=workspace.branch,
            base_branch="main",
            title="persisted",
            number=13,
            run_id=run.run_id,
        )
    )
    publisher = FakeWorkspacePublisher()

    with pytest.raises(PullRequestIdentityError):
        _service(db_path, publisher=publisher).publish(task.task_id, run.run_id)

    assert publisher.calls == 0
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_persisted_pr_for_another_run_is_refused(db_path: str, tmp_path: Path) -> None:
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None

    # A different run (of a different task, same target repository) is the one
    # that actually owns the persisted PR, sharing head branch text. Publishing
    # this run must not adopt it.
    other_task = tasks.save(
        FactoryTask(
            title="Other",
            target_repository="example/target",
            source=TaskSource("github", "example/control", 8),
        )
    )
    other_run = _validated_run(_runs(db_path), other_task, tmp_path, run_id="run-2")
    _prs(db_path).save(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="main",
            title="persisted",
            number=14,
            task_id=other_task.task_id,
            run_id=other_run.run_id,
        )
    )
    publisher = FakeWorkspacePublisher()

    with pytest.raises(PullRequestIdentityError):
        _service(db_path, publisher=publisher).publish(task.task_id, run.run_id)

    assert publisher.calls == 0
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING


def test_persisted_pr_identity_error_is_sanitized(db_path: str, tmp_path: Path) -> None:
    secret = "MY_PRIVATE_PUSH_PASSWORD_93726"
    tasks = _tasks(db_path)
    task = _task(tasks)
    run = _validated_run(_runs(db_path), task, tmp_path)
    workspace = run.workspace
    assert workspace is not None
    _prs(db_path).save(
        PullRequest(
            repository_slug=workspace.repository_slug,
            head_branch=workspace.branch,
            base_branch="release",
            title=secret,
            body=secret,
            number=15,
            url=f"https://example.invalid/{secret}",
            run_id=run.run_id,
        )
    )

    with pytest.raises(PullRequestIdentityError) as caught:
        _service(db_path).publish(task.task_id, run.run_id)

    error = caught.value
    formatted = "".join(traceback.format_exception(error))
    for text in (secret, "release", "https://", "example.invalid"):
        assert text not in str(error)
        assert text not in repr(error)
        assert text not in formatted
    assert error.__cause__ is None and error.__context__ is None
