"""Project identity stays bound across backlog, persistence and context."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from factory.domain.backlog import MaterializedIssue, WorkItem
from factory.domain.enums import AgentKind
from factory.domain.models import AgentRun, FactoryTask, QualityGateSpec, TaskSource, new_workspace
from factory.domain.projects import ProjectProfile, ProjectRegistry, ProjectRoutingError
from factory.infrastructure.config import FactoryConfig
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.backlog_sqlite import SqliteBacklogLinkRepository
from factory.infrastructure.persistence.run_sqlite import SqliteRunRepository
from factory.infrastructure.persistence.schema import CREATE_TASKS
from factory.infrastructure.persistence.sqlite import SqliteTaskRepository
from factory.integrations.project_routing import ProjectContextSource, ProjectWorkspaceProvisioner
from factory.integrations.trello.backlog import TrelloBacklogSource
from factory.orchestration.backlog import BacklogMaterializationService
from factory.orchestration.context import TaskContextSource


def _registry(tmp_path: Path) -> ProjectRegistry:
    return ProjectRegistry(
        (
            ProjectProfile(
                "factory",
                "example/factory",
                str(tmp_path / "factory"),
                "main",
                (QualityGateSpec("lint", ("ruff", "check", ".")),),
            ),
            ProjectProfile(
                "dulces",
                "example/dulces",
                str(tmp_path / "dulces"),
                "release",
                (QualityGateSpec("tests", ("pytest",)),),
            ),
        )
    )


class _Source:
    def __init__(self, item: WorkItem) -> None:
        self.item = item

    def list_items(self) -> list[WorkItem]:
        return [self.item]

    def get_item(self, external_id: str) -> WorkItem:
        assert external_id == self.item.external_id
        return self.item


class _Sink:
    def __init__(self) -> None:
        self.posts: list[str] = []

    def find_issue(self, item: WorkItem) -> MaterializedIssue | None:
        return None

    def create_issue(self, item: WorkItem) -> MaterializedIssue:
        self.posts.append(item.target_repository)
        return MaterializedIssue(
            item.target_repository,
            len(self.posts),
            "https://github.com/" + item.target_repository + "/issues/1",
        )


def test_two_projects_route_issues_and_reject_forgery_durably(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    links = SqliteBacklogLinkRepository(str(tmp_path / "db"))
    links.initialize()
    sink = _Sink()
    for project, repo in (("factory", "example/factory"), ("dulces", "example/dulces")):
        item = WorkItem("trello", project, "Title", "body", repo, True, True, project)
        summary = BacklogMaterializationService(_Source(item), sink, links, registry).reconcile()
        assert summary.created == 1
    assert sink.posts == ["example/factory", "example/dulces"]
    forged = WorkItem("trello", "forged", "Title", "body", "example/factory", True, True, "dulces")
    unknown = replace(forged, external_id="unknown", project_id="missing")
    for item in (forged, unknown):
        result = BacklogMaterializationService(_Source(item), sink, links, registry).reconcile()
        assert result.ineligible == 1
    assert len(sink.posts) == 2
    with sqlite3.connect(links.path) as conn:
        assert conn.execute("SELECT count(*) FROM routing_rejections").fetchone()[0] == 2


def test_project_identity_survives_restart_and_appears_in_trace(tmp_path: Path) -> None:
    path = str(tmp_path / "db")
    tasks = SqliteTaskRepository(path)
    runs = SqliteRunRepository(path)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(
        FactoryTask(
            "Title",
            "example/dulces",
            source=TaskSource("github", "example/dulces", 7),
            project_id="dulces",
        )
    )
    run = runs.save_run(AgentRun(task.task_id, AgentKind.CODEX, project_id="dulces"))
    assert SqliteTaskRepository(path).get(task.task_id).project_id == "dulces"
    assert SqliteRunRepository(path).get_run(run.run_id).project_id == "dulces"
    assert SqliteAuditEventStore(path).for_task(task.task_id)[0].project_id == "dulces"
    with pytest.raises(KeyError):
        tasks.update(replace(task, project_id="factory"))
    with pytest.raises(ValueError):
        _registry(tmp_path).resolve("dulces", "example/factory")
    content = TaskContextSource().fragments(task)[0].content
    assert "Project ID: dulces" in content
    assert "Target repository: example/dulces" in content


def test_registry_rejects_duplicate_repository(tmp_path: Path) -> None:
    profile = _registry(tmp_path).profiles[0]
    with pytest.raises(ProjectRoutingError):
        ProjectRegistry((profile, replace(profile, project_id="other")))


def test_old_task_table_migrates_project_identity(tmp_path: Path) -> None:
    path = str(tmp_path / "legacy.db")
    old_schema = CREATE_TASKS.replace(
        "    project_id           TEXT NOT NULL DEFAULT 'ai-factory-lab',\n", ""
    )
    with sqlite3.connect(path) as conn:
        conn.execute(old_schema)
        conn.execute(
            "INSERT INTO tasks (task_id, title, target_repository, status, created_at, "
            "updated_at) VALUES ('old', 'Old', 'example/factory', 'READY', '2026-01-01', "
            "'2026-01-01')"
        )
    tasks = SqliteTaskRepository(path)
    tasks.initialize()
    loaded = tasks.get("old")
    assert loaded is not None and loaded.project_id == "ai-factory-lab"


def test_legacy_waiting_human_restart_matches_canonical_control_plane_profile(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "legacy-waiting.db")
    old_schema = CREATE_TASKS.replace(
        "    project_id           TEXT NOT NULL DEFAULT 'ai-factory-lab',\n", ""
    )
    with sqlite3.connect(path) as conn:
        conn.execute(old_schema)
        conn.execute(
            "INSERT INTO tasks (task_id, title, target_repository, status, created_at, "
            "updated_at) VALUES ('legacy-waiting', 'Legacy waiting', "
            "'cartunduaga06/ai-factory-lab', 'WAITING_HUMAN', '2026-01-01', '2026-01-01')"
        )

    SqliteTaskRepository(path).initialize()
    restarted = SqliteTaskRepository(path)
    loaded = restarted.get("legacy-waiting")
    assert loaded is not None
    assert loaded.status.value == "WAITING_HUMAN"
    assert loaded.project_id == "ai-factory-lab"

    registry = ProjectRegistry(
        (
            ProjectProfile(
                "ai-factory-lab",
                "cartunduaga06/ai-factory-lab",
                str(tmp_path / "factory"),
                "main",
                (QualityGateSpec("tests", ("pytest",)),),
            ),
        )
    )
    profile = registry.resolve(loaded.project_id, loaded.target_repository)
    assert profile.repository_slug == "cartunduaga06/ai-factory-lab"


def test_operator_configuration_loads_two_project_profiles(tmp_path: Path) -> None:
    profiles = [
        {
            "project_id": p.project_id,
            "repository": p.repository_slug,
            "source_checkout": p.source_checkout,
            "base_ref": p.base_ref,
            "gates": [{"name": gate.name, "argv": list(gate.argv)} for gate in p.gates],
        }
        for p in _registry(tmp_path).profiles
    ]
    config = FactoryConfig.from_env({"FACTORY_PROJECTS": json.dumps(profiles)})
    assert config.project_registry is not None
    assert config.project_registry.resolve("dulces").repository_slug == "example/dulces"
    with pytest.raises(ValueError, match="FACTORY_PROJECTS"):
        FactoryConfig.from_env({"FACTORY_PROJECTS": json.dumps(profiles + [profiles[0]])})


def _checkout(path: Path, slug: str, label: str) -> None:
    path.mkdir()
    for args in (
        ("init", "-b", "main"),
        ("config", "user.name", "Test"),
        ("config", "user.email", "test@example.com"),
        ("remote", "add", "origin", f"https://github.com/{slug}.git"),
    ):
        subprocess.run(("git", *args), cwd=path, check=True, capture_output=True)
    (path / "AGENTS.md").write_text(f"Instructions for {label}\n")
    (path / "README.md").write_text(f"Project {label}\n")
    subprocess.run(("git", "add", "."), cwd=path, check=True, capture_output=True)
    subprocess.run(("git", "commit", "-m", "initial"), cwd=path, check=True, capture_output=True)


def test_project_context_and_workspaces_are_separate(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    _checkout(tmp_path / "factory", "example/factory", "factory")
    _checkout(tmp_path / "dulces", "example/dulces", "dulces")
    source = ProjectContextSource(registry)
    provisioner = ProjectWorkspaceProvisioner(registry)
    tasks = (
        FactoryTask("Factory task", "example/factory", project_id="factory"),
        FactoryTask("Dulces task", "example/dulces", project_id="dulces"),
    )
    contents = [source.fragments(task)[0].content for task in tasks]
    assert "factory" in contents[0] and "dulces" not in contents[0]
    assert "dulces" in contents[1] and "factory" not in contents[1]
    first = new_workspace(tasks[0], str(tmp_path / "workspaces"))
    provisioner.prepare(tasks[0], first)
    assert Path(first.path).is_dir()
    with pytest.raises(ProjectRoutingError):
        source.fragments(replace(tasks[1], target_repository="example/factory"))
    with pytest.raises(ValueError):
        provisioner.prepare(tasks[1], first)


class _TrelloTransport:
    def __init__(self, body: str) -> None:
        self.body = body

    def get_json(self, url: str, headers: object) -> object:
        if url.split("?", 1)[0].endswith("/checklists"):
            return []
        return {
            "id": "abc123",
            "idList": "list123",
            "closed": False,
            "name": "Work",
            "desc": self.body,
            "idLabels": ["ready123"],
        }


def test_trello_only_selects_registered_project_id(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    source = TrelloBacklogSource(
        "key",
        "token",
        "list123",
        "ready123",
        "example/factory",
        _TrelloTransport("project_id: dulces"),
        registry=registry,
    )
    item = source.get_item("abc123")
    assert (item.project_id, item.target_repository) == ("dulces", "example/dulces")
    forged = TrelloBacklogSource(
        "key",
        "token",
        "list123",
        "ready123",
        "example/factory",
        _TrelloTransport("project_id: dulces\ntarget_repository: example/factory"),
        registry=registry,
    ).get_item("abc123")
    assert forged.target_repository == "invalid/invalid"
