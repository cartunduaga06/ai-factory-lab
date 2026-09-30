"""Sprint authorization guards E2 and task selection across restarts."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

import factory.__main__ as cli
from factory.domain.backlog import MaterializedIssue, WorkItem
from factory.domain.enums import AgentKind, RepositoryRole, RunStatus, TaskStatus
from factory.domain.models import FactoryTask, PullRequest, Repository, TaskSource
from factory.domain.ports import PullRequestState, PullRequestStateSource
from factory.domain.sprint import SprintState
from factory.infrastructure.config import FactoryConfig
from factory.infrastructure.persistence import SqlitePullRequestRepository, SqliteRunRepository
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.backlog_sqlite import SqliteBacklogLinkRepository
from factory.infrastructure.persistence.sprint_sqlite import SqliteSprintRepository
from factory.infrastructure.persistence.sqlite import SqliteTaskRepository
from factory.orchestration.backlog import BacklogMaterializationService
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.runtime import FactoryRuntime
from factory.orchestration.sprint import AuthorizedBacklogSource, SprintService
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeQualityGateRunner,
    FakeRevisionInspector,
    FakeWorkspaceProvisioner,
    specs,
)


class Source:
    def __init__(self) -> None:
        self.items = {
            key: WorkItem("trello", key, f"Item {key}", "body", "example/control", True, True)
            for key in ("a", "b", "outside")
        }

    def list_items(self) -> list[WorkItem]:
        return list(self.items.values())

    def get_item(self, external_id: str) -> WorkItem:
        return self.items[external_id]


class Sink:
    def __init__(self) -> None:
        self.issues: dict[str, MaterializedIssue] = {}
        self.posts = 0

    def find_issue(self, item: WorkItem) -> MaterializedIssue | None:
        return self.issues.get(item.external_id)

    def create_issue(self, item: WorkItem) -> MaterializedIssue:
        self.posts += 1
        issue = MaterializedIssue(
            "example/control", self.posts, f"https://github.com/example/control/issues/{self.posts}"
        )
        self.issues[item.external_id] = issue
        return issue


def _service(path: Path, source: Source, sink: Sink) -> tuple[SprintService, SqliteTaskRepository]:
    tasks = SqliteTaskRepository(str(path))
    links = SqliteBacklogLinkRepository(str(path))
    sprints = SqliteSprintRepository(str(path))
    tasks.initialize()
    materializer = BacklogMaterializationService(
        AuthorizedBacklogSource(source, sprints), sink, links
    )
    return SprintService(source, materializer, links, tasks, sprints), tasks


def test_authorized_order_pause_resume_and_trace_survive_restart(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    source, sink = Source(), Sink()
    service, tasks = _service(path, source, sink)
    manifest = service.draft("sprint-1", (("a", ()), ("b", ("trello:a",))))
    assert [row.blockers for row in service.plan(manifest)] == [
        (),
        ("ordered_predecessor_waiting",),
    ]
    assert sink.posts == 0
    assert service.prepare() is False
    service.authorize(manifest)
    service.authorize(manifest)
    with pytest.raises(ValueError, match="immutable"):
        service.authorize(replace(manifest, steps=(manifest.steps[0],)))
    assert service.prepare() is True
    assert sink.posts == 1
    assert service.prepare() is True
    assert sink.posts == 1
    outside = FactoryTask(
        "Outside",
        target_repository="example/product",
        source=TaskSource("github", "example/control", 99),
        status=TaskStatus.READY,
    )
    tasks.save(outside)
    assert service.allows(outside) is False
    first = FactoryTask(
        "First",
        target_repository="example/product",
        source=TaskSource("github", "example/control", 1),
        status=TaskStatus.READY,
    )
    tasks.save(first)
    assert service.allows(first) is True
    waiting = tasks.apply_transition(first.task_id, TaskStatus.READY, TaskStatus.WAITING_HUMAN)
    service.observe(waiting)
    assert service.is_paused()
    assert service.prepare() is False
    with pytest.raises(ValueError, match="gate"):
        service.resume("sprint-1")

    restarted, tasks = _service(path, source, sink)
    assert restarted.is_paused()
    tasks.apply_transition(first.task_id, TaskStatus.WAITING_HUMAN, TaskStatus.DONE)
    restarted.resume("sprint-1")
    assert restarted.prepare() is True
    assert sink.posts == 2
    assert SqliteSprintRepository(str(path)).current()[1:] == (SprintState.ACTIVE, 1)  # type: ignore[index]
    assert [event.name for event in SqliteAuditEventStore(str(path)).for_sprint("sprint-1")] == [
        "SprintAuthorized",
        "WorkItemSelected",
        "SprintPaused",
        "SprintResumed",
        "SprintAdvanced",
        "WorkItemSelected",
    ]


def test_changed_snapshot_pauses_before_e2_write(tmp_path: Path) -> None:
    source, sink = Source(), Sink()
    service, _ = _service(tmp_path / "factory.db", source, sink)
    manifest = service.draft("sprint-2", (("a", ()),))
    service.authorize(manifest)
    source.items["a"] = replace(source.items["a"], body="changed")
    assert service.prepare() is False
    assert service.is_paused()
    assert sink.posts == 0


def test_dependency_requires_predecessor_done_even_if_position_was_advanced(
    tmp_path: Path,
) -> None:
    path = tmp_path / "factory.db"
    source, sink = Source(), Sink()
    service, tasks = _service(path, source, sink)
    manifest = service.draft("sprint-deps", (("a", ()), ("b", ("trello:a",))))
    service.authorize(manifest)
    assert service.prepare() is True
    predecessor = tasks.save(
        FactoryTask(
            "predecessor",
            "example/control",
            source=TaskSource("github", "example/control", 1),
            status=TaskStatus.READY,
        )
    )
    # Simulate an interrupted or divergent position update. The dependency
    # resolver must use the durable task outcome, not the position alone.
    SqliteSprintRepository(str(path)).move(
        manifest.sprint_id, SprintState.ACTIVE, 1, "SprintAdvanced"
    )
    assert service.prepare() is False
    assert service.is_paused()
    assert sink.posts == 1
    with pytest.raises(ValueError, match="dependencies"):
        service.resume(manifest.sprint_id)
    assert tasks.get(predecessor.task_id).status is TaskStatus.READY  # type: ignore[union-attr]


def test_dry_run_reports_ineligible_and_repeated_human_pauses_are_traced(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    source, sink = Source(), Sink()
    service, _ = _service(path, source, sink)
    source.items["a"] = replace(source.items["a"], eligible=False)
    manifest = service.draft("sprint-4", (("a", ()),))
    assert service.plan(manifest)[0].blockers == ("source_ineligible",)
    with pytest.raises(ValueError, match="blockers"):
        service.authorize(manifest)
    assert SqliteSprintRepository(str(path)).current() is None
    assert SqliteAuditEventStore(str(path)).for_sprint("sprint-4") == ()
    invalid = service.draft("sprint-invalid", (("a", ("trello:b",)), ("a", ())))
    assert service.plan(invalid)[0].blockers == ("dependency_not_preceding", "source_ineligible")
    assert "duplicate_work_item" in service.plan(invalid)[1].blockers
    source.items["a"] = replace(source.items["a"], eligible=True)
    service.authorize(service.draft("sprint-4", (("a", ()),)))
    service.pause("sprint-4")
    service.resume("sprint-4")
    service.pause("sprint-4")
    service.resume("sprint-4")
    assert [event.name for event in SqliteAuditEventStore(str(path)).for_sprint("sprint-4")] == [
        "SprintAuthorized",
        "SprintPaused",
        "SprintResumed",
        "SprintPaused",
        "SprintResumed",
    ]


@pytest.mark.parametrize("action", ["pause", "request-human"])
def test_sprint_cli_human_decisions_pause_without_execution(
    action: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "factory.db"
    source, sink = Source(), Sink()
    service, tasks = _service(path, source, sink)
    service.authorize(service.draft("sprint-cli", (("a", ()),)))
    config = FactoryConfig.from_env({"DATABASE_URL": f"sqlite:///{path}"})
    monkeypatch.setattr(cli.FactoryConfig, "from_env", lambda: config)
    monkeypatch.setattr(cli, "_build_sprint", lambda _config, _tasks: service)

    def forbidden_runtime(_config: FactoryConfig) -> None:
        raise AssertionError("a sprint decision must not start the factory runtime")

    monkeypatch.setattr(cli, "_run_runtime", forbidden_runtime)
    monkeypatch.setattr(cli, "_run_watch", forbidden_runtime)

    parsed = cli.build_parser().parse_args(["sprint", action, "--sprint-id", "sprint-cli"])
    assert parsed.action == action
    assert cli.main(["sprint", action, "--sprint-id", "sprint-cli"]) == cli.EXIT_OK
    assert "Sprint paused for human decision" in capsys.readouterr().out
    assert SqliteSprintRepository(str(path)).current()[1:] == (SprintState.PAUSED, 0)  # type: ignore[index]
    assert [event.name for event in SqliteAuditEventStore(str(path)).for_sprint("sprint-cli")] == [
        "SprintAuthorized",
        "SprintPaused",
    ]
    assert sink.posts == 0
    assert tasks.list() == []
    assert SqliteRunRepository(str(path)).list_runs() == []


@pytest.mark.parametrize("action", ["pause", "request-human"])
def test_sprint_cli_human_decisions_fail_closed_without_authorization(
    action: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "factory.db"
    source, sink = Source(), Sink()
    service, tasks = _service(path, source, sink)
    config = FactoryConfig.from_env({"DATABASE_URL": f"sqlite:///{path}"})
    monkeypatch.setattr(cli.FactoryConfig, "from_env", lambda: config)
    monkeypatch.setattr(cli, "_build_sprint", lambda _config, _tasks: service)

    assert cli.main(["sprint", action, "--sprint-id", "unknown"]) == cli.EXIT_CONFIG_ERROR
    assert "sprint refused: ValueError" in capsys.readouterr().out
    assert SqliteSprintRepository(str(path)).current() is None
    assert sink.posts == 0
    assert tasks.list() == []


def test_runtime_never_dispatches_issue_outside_authorized_sprint(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    source, sink = Source(), Sink()
    sprint, tasks = _service(path, source, sink)
    runs = SqliteRunRepository(str(path))
    prs = SqlitePullRequestRepository(str(path))
    inside = FactoryTask(
        "Inside",
        target_repository="example/product",
        source=TaskSource("github", "example/control", 1),
    )
    outside = FactoryTask(
        "Outside",
        target_repository="example/product",
        source=TaskSource("github", "example/control", 99),
    )

    class Issues:
        def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
            return [outside, inside]

        def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
            return inside

        def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
            return True

    runtime = FactoryRuntime(
        intake=IssueIntakeService(Issues(), tasks),
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=FakeAgentAdapter(kind=AgentKind.OTHER, status=RunStatus.SUCCEEDED),
        provisioner=FakeWorkspaceProvisioner(),
        workspace_root=str(tmp_path / "workspaces"),
        gate_specs=specs("tests"),
        gate_runner=FakeQualityGateRunner(),
        revision_inspector=FakeRevisionInspector(),
        publisher=FakeWorkspacePublisher(),
        pull_request_sink=FakePullRequestSink(),
        pull_requests=prs,
        base_branch="main",
        poll_interval=0,
        timeout=1,
        sprint=sprint,
    )
    assert runtime.run_once().outcome == "NO_ELIGIBLE_TASK"
    assert tasks.list() == []
    sprint.authorize(sprint.draft("sprint-3", (("a", ()),)))
    result = runtime.run_once()
    assert result.task_status is TaskStatus.WAITING_HUMAN
    assert result.task_id == tasks.find_by_source(inside.source).task_id  # type: ignore[arg-type,union-attr]
    assert tasks.find_by_source(outside.source) is not None  # type: ignore[arg-type]
    assert runs.list_runs(tasks.find_by_source(outside.source).task_id) == []  # type: ignore[arg-type,union-attr]
    assert sprint.is_paused()

    class Merged(PullRequestStateSource):
        def state(self, pull_request: PullRequest) -> PullRequestState:
            return PullRequestState.MERGED

    runtime._pull_request_state = Merged()
    assert runtime.run_once().outcome == "SPRINT_PAUSED"
    assert tasks.get(result.task_id).status is TaskStatus.DONE  # type: ignore[arg-type,union-attr]
    assert sprint.is_paused()
    sprint.resume("sprint-3")
    assert runtime.run_once().outcome == "NO_ELIGIBLE_TASK"
    assert SqliteSprintRepository(str(path)).current()[1] is SprintState.COMPLETE  # type: ignore[index]
