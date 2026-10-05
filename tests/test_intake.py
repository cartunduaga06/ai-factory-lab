"""Tests for the intake service.

Intake is provider-agnostic, so these tests drive a fake ``IssueSource`` while
persisting to a real SQLite repository. They prove the idempotency guarantee:
repeated intake over the same issues creates no duplicates and leaves existing
tasks untouched.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from factory.domain.enums import RepositoryRole, TaskStatus
from factory.domain.errors import ProviderRequestError
from factory.domain.models import FactoryTask, QualityGateSpec, Repository, TaskSource
from factory.domain.ports import IssueSource
from factory.domain.projects import ProjectProfile, ProjectRegistry
from factory.infrastructure.persistence import SqliteTaskRepository
from factory.integrations.github.project_issues import ProjectIssueSource
from factory.orchestration.intake import IssueIntakeService

REPO = Repository(slug="cartunduaga06/ai-factory-lab", role=RepositoryRole.CONTROL_PLANE)


class FakeIssueSource(IssueSource):
    """Deterministic in-memory source standing in for GitHub."""

    def __init__(self, tasks: list[FactoryTask]) -> None:
        self._tasks = tasks
        self.list_calls = 0

    def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
        self.list_calls += 1
        return list(self._tasks)

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        for task in self._tasks:
            if task.source == source:
                return task
        raise KeyError(source)

    def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
        return any(task.source == source for task in self._tasks)


@pytest.fixture
def repo(tmp_path: Path) -> SqliteTaskRepository:
    repository = SqliteTaskRepository(str(tmp_path / "factory.db"))
    repository.initialize()
    return repository


def _task(number: int, *, title: str | None = None) -> FactoryTask:
    return FactoryTask(
        title=title or f"Issue {number}",
        target_repository="cartunduaga06/finanza-ia",
        source=TaskSource("github", "cartunduaga06/ai-factory-lab", number),
        labels=("factory-ready",),
    )


def test_new_issue_creates_task(repo: SqliteTaskRepository) -> None:
    source = FakeIssueSource([_task(10)])
    summary = IssueIntakeService(source, repo).intake(REPO)

    assert summary.discovered == 1
    assert summary.created == 1
    assert summary.existing == 0
    assert summary.errors == 0
    assert repo.find_by_source(_task(10).source) is not None  # type: ignore[arg-type]


def test_multiple_issues_create_tasks(repo: SqliteTaskRepository) -> None:
    source = FakeIssueSource([_task(10), _task(11), _task(12)])
    summary = IssueIntakeService(source, repo).intake(REPO)

    assert (summary.discovered, summary.created, summary.existing) == (3, 3, 0)
    assert len(repo.list()) == 3


def test_second_intake_creates_no_duplicates(repo: SqliteTaskRepository) -> None:
    source = FakeIssueSource([_task(10), _task(11), _task(12)])
    service = IssueIntakeService(source, repo)

    first = service.intake(REPO)
    second = service.intake(REPO)

    assert (first.created, first.existing) == (3, 0)
    assert (second.discovered, second.created, second.existing) == (3, 0, 3)
    assert len(repo.list()) == 3


def test_existing_task_is_left_unchanged(repo: SqliteTaskRepository) -> None:
    source = FakeIssueSource([_task(10)])
    service = IssueIntakeService(source, repo)
    service.intake(REPO)

    before = repo.find_by_source(_task(10).source)  # type: ignore[arg-type]
    assert before is not None
    original_id = before.task_id
    original_status = before.status

    service.intake(REPO)

    after = repo.find_by_source(_task(10).source)  # type: ignore[arg-type]
    assert after is not None
    assert after.task_id == original_id
    assert after.status is original_status
    assert after.created_at == before.created_at


def test_task_without_source_identity_is_counted_as_error(repo: SqliteTaskRepository) -> None:
    source = FakeIssueSource(
        [FactoryTask(title="no source", target_repository="cartunduaga06/finanza-ia")]
    )
    summary = IssueIntakeService(source, repo).intake(REPO)
    assert summary.discovered == 1
    assert summary.created == 0
    assert summary.errors == 1
    assert repo.list() == []


def test_one_bad_task_does_not_stop_the_rest(repo: SqliteTaskRepository) -> None:
    source = FakeIssueSource(
        [
            _task(10),
            FactoryTask(title="bad", target_repository="cartunduaga06/finanza-ia"),
            _task(11),
        ]
    )
    summary = IssueIntakeService(source, repo).intake(REPO)
    assert (summary.discovered, summary.created, summary.errors) == (3, 2, 1)
    assert len(repo.list()) == 2


def test_summary_matches_the_documented_example(repo: SqliteTaskRepository) -> None:
    # Issue #10 already present, #11 and #12 new -> discovered 3, created 2,
    # existing 1, mirroring the specification's worked example.
    repo.save(_task(10))
    source = FakeIssueSource([_task(10), _task(11), _task(12)])
    summary = IssueIntakeService(source, repo).intake(REPO)

    assert summary.discovered == 3
    assert summary.created == 2
    assert summary.existing == 1
    assert summary.errors == 0
    assert len(repo.list()) == 3


def test_service_is_provider_agnostic(repo: SqliteTaskRepository) -> None:
    # A non-GitHub provider flows through the same service untouched.
    foreign = FactoryTask(
        title="GitLab task",
        target_repository="cartunduaga06/finanza-ia",
        source=TaskSource("gitlab", "cartunduaga06/ai-factory-lab", 5),
    )
    summary = IssueIntakeService(FakeIssueSource([foreign]), repo).intake(REPO)
    assert summary.created == 1
    assert repo.find_by_source(foreign.source) is not None  # type: ignore[arg-type]


def test_intake_does_not_transition_task_status(repo: SqliteTaskRepository) -> None:
    # Intake only persists; lifecycle movement is a separate concern.
    summary = IssueIntakeService(FakeIssueSource([_task(1)]), repo).intake(REPO)
    assert summary.created == 1
    stored = repo.list()[0]
    assert stored.status is TaskStatus.DISCOVERED


def test_registered_multi_repo_intake_routes_and_is_idempotent(repo: SqliteTaskRepository) -> None:
    slug = "cartunduaga06/finanza-ia"
    incoming = FactoryTask(
        title="Issue 158",
        target_repository=slug,
        source=TaskSource("github", slug, 158),
        labels=("factory-ready",),
    )
    profile = ProjectProfile(
        project_id="finanza-ia",
        repository_slug=slug,
        source_checkout="/work/finanza-ia",
        base_ref="main",
        gates=(QualityGateSpec("pytest", ("pytest",), True),),
    )
    source = ProjectIssueSource(FakeIssueSource([incoming]), ProjectRegistry((profile,)))
    service = IssueIntakeService(source, repo)
    first = service.intake(REPO)
    second = service.intake(REPO)
    saved = repo.find_by_source(incoming.source)
    assert (first.discovered, first.created, second.created, second.existing) == (1, 1, 0, 1)
    assert saved is not None
    assert (saved.project_id, saved.target_repository) == ("finanza-ia", slug)


def test_registered_intake_defers_one_unavailable_project(
    repo: SqliteTaskRepository, caplog: pytest.LogCaptureFixture
) -> None:
    available = "cartunduaga06/ai-factory-lab"
    unavailable = "cartunduaga06/finanza-ia"
    incoming = FactoryTask(
        title="Available issue",
        target_repository=available,
        source=TaskSource("github", available, 150),
        labels=("factory-ready",),
    )

    class PartiallyUnavailableSource(FakeIssueSource):
        def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
            if repository.slug == unavailable:
                raise ProviderRequestError("provider unavailable")
            return [incoming] if repository.slug == available else []

    profiles = (
        ProjectProfile(
            project_id="ai-factory-lab",
            repository_slug=available,
            source_checkout="/work/ai-factory-lab",
            base_ref="main",
            gates=(QualityGateSpec("pytest", ("pytest",), True),),
        ),
        ProjectProfile(
            project_id="finanza-ia",
            repository_slug=unavailable,
            source_checkout="/work/finanza-ia",
            base_ref="main",
            gates=(QualityGateSpec("pytest", ("pytest",), True),),
        ),
    )

    with caplog.at_level("WARNING"):
        summary = IssueIntakeService(
            ProjectIssueSource(PartiallyUnavailableSource([]), ProjectRegistry(profiles)), repo
        ).intake(REPO)

    saved = repo.find_by_source(incoming.source)
    assert (summary.discovered, summary.created, summary.errors) == (1, 1, 0)
    assert saved is not None
    assert saved.project_id == "ai-factory-lab"
    assert "project intake deferred: project_id=finanza-ia" in caplog.text
    assert "error_type=ProviderRequestError" in caplog.text


def test_registered_intake_rejects_unregistered_source(repo: SqliteTaskRepository) -> None:
    foreign = FactoryTask(
        title="Unregistered",
        target_repository="other/project",
        source=TaskSource("github", "other/project", 9),
    )
    profile = ProjectProfile(
        project_id="finanza-ia",
        repository_slug="cartunduaga06/finanza-ia",
        source_checkout="/work/finanza-ia",
        base_ref="main",
        gates=(QualityGateSpec("pytest", ("pytest",), True),),
    )
    source = ProjectIssueSource(FakeIssueSource([foreign]), ProjectRegistry((profile,)))
    with pytest.raises(ValueError, match="unregistered project repository"):
        IssueIntakeService(source, repo).intake(REPO)
    assert repo.list() == []
