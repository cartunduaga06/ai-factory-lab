"""Cross-layer test: only the validated workspace revision may be published.

This is the Phase 5 integrity regression. It wires the real orchestration
services, the real Git revision inspector and the real Git publisher against a
disposable repository, so the whole chain is exercised:

```
agent succeeds -> gates pass -> validated_revision bound
              -> workspace changes -> publication refuses
```

No network, no real product repository, no real credential.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus, TaskStatus
from factory.domain.errors import ValidatedRevisionMismatchError
from factory.domain.models import AgentRun, FactoryTask, Workspace
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.integrations.workspace.git_publish import GitWorkspacePublisher
from factory.integrations.workspace.revision import GitWorkspaceRevisionInspector
from factory.orchestration import PublicationService, RunTrackingService
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink
from tests.fake_workspace import FakeQualityGateRunner, specs

_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        **_GIT_IDENTITY,
    }


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, env=_env(cwd)
    )


def _source_repo(tmp_path: Path) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    _git(source, "init", "-b", "main")
    (source / "README.md").write_text("hello\n", encoding="utf-8")
    _git(source, "add", "README.md")
    _git(source, "commit", "-m", "initial")
    return source


def _setup(tmp_path: Path) -> tuple[Path, Path, FactoryTask, AgentRun]:
    """Build a disposable repo, worktree, VALIDATING task and SUCCEEDED run."""
    db_path = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(db_path)
    tasks.initialize()
    runs = SqliteRunRepository(db_path)
    runs.initialize()

    source = _source_repo(tmp_path)
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git(remote, "init", "--bare", "-b", "main")
    _git(source, "remote", "add", "origin", str(remote))

    task = tasks.save(
        FactoryTask(
            title="Task",
            target_repository="example/target",
            source=None,
        )
    )
    for frm, to in (
        (TaskStatus.DISCOVERED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
        (TaskStatus.CLAIMED, TaskStatus.RUNNING),
    ):
        tasks.apply_transition(task.task_id, frm, to)

    branch = f"factory/{task.task_id}/ws-1"
    worktree = tmp_path / "ws-1"
    _git(source, "worktree", "add", "-b", branch, str(worktree))
    workspace = Workspace(repository_slug="example/target", branch=branch, path=str(worktree))
    run = AgentRun(
        task_id=task.task_id,
        adapter=AgentKind.OTHER,
        run_id="run-1",
        status=RunStatus.RUNNING,
        workspace=workspace,
    )
    runs.save_run(run)
    return source, remote, task, run


def _repos(
    db_path: str,
) -> tuple[SqliteTaskRepository, SqliteRunRepository, SqlitePullRequestRepository]:
    tasks = SqliteTaskRepository(db_path)
    tasks.initialize()
    runs = SqliteRunRepository(db_path)
    runs.initialize()
    prs = SqlitePullRequestRepository(db_path)
    prs.initialize()
    return tasks, runs, prs


def _validate(db_path: str, task: FactoryTask, run: AgentRun) -> AgentRun:
    """Run one validation pass with the real inspector and a green gate."""
    service = RunTrackingService(
        SqliteTaskRepository(db_path),
        SqliteRunRepository(db_path),
        gate_specs=specs("tests"),
        gate_runner=FakeQualityGateRunner(statuses={"tests": QualityGateStatus.PASSED}),
        revision_inspector=GitWorkspaceRevisionInspector(),
    )
    result = service.refresh(run.run_id, FakeAgentAdapter(collect_status=RunStatus.SUCCEEDED))
    assert result.run.validated_revision is not None
    return result.run


def _publish(
    db_path: str, task: FactoryTask, run: AgentRun
) -> tuple[PublicationService, FakePullRequestSink]:
    sink = FakePullRequestSink()
    service = PublicationService(
        SqliteTaskRepository(db_path),
        SqliteRunRepository(db_path),
        SqlitePullRequestRepository(db_path),
        publisher=GitWorkspacePublisher(),
        sink=sink,
        base_branch="main",
        default_branch="main",
    )
    service.publish(task.task_id, run.run_id)
    return service, sink


def _head(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD").stdout.decode().strip()


def _tree(path: Path) -> str:
    return _git(path, "rev-parse", "HEAD^{tree}").stdout.decode().strip()


def test_happy_path_publishes_exactly_the_validated_revision(tmp_path: Path) -> None:
    source, remote, task, run = _setup(tmp_path)
    db_path = str(tmp_path / "factory.db")
    worktree = Path(run.workspace.path)  # type: ignore[union-attr]
    (worktree / "impl.py").write_text("A\n", encoding="utf-8")

    validated = _validate(db_path, task, run)
    assert validated.validated_revision is not None
    revision = validated.validated_revision

    _publish(db_path, task, validated)

    # The commit's tree is exactly the tree that passed validation.
    assert _tree(worktree) == revision
    branches = _git(remote, "branch", "--list", "--format=%(refname:short)").stdout.decode()
    assert branches.strip() == validated.workspace.branch  # type: ignore[union-attr]
    tasks, _, prs = _repos(db_path)
    assert prs.get_for_run(run.run_id) is not None
    assert tasks.get(task.task_id).status is TaskStatus.WAITING_HUMAN
    history = [t.to_status for t in tasks.history(task.task_id)]
    assert history[-2:] == [TaskStatus.PR_OPEN, TaskStatus.WAITING_HUMAN]


def test_post_validation_mutation_blocks_publication(tmp_path: Path) -> None:
    # The core regression: gates passed, revision bound, then a file changed.
    source, remote, task, run = _setup(tmp_path)
    db_path = str(tmp_path / "factory.db")
    worktree = Path(run.workspace.path)  # type: ignore[union-attr]
    (worktree / "impl.py").write_text("validated\n", encoding="utf-8")

    validated = _validate(db_path, task, run)
    revision = validated.validated_revision
    head_before = _head(worktree)

    # AFTER validation: modify, add and delete.
    (worktree / "impl.py").write_text("tampered\n", encoding="utf-8")
    (worktree / "extra.py").write_text("sneaky\n", encoding="utf-8")
    (worktree / "README.md").unlink()

    sink = FakePullRequestSink()
    service = PublicationService(
        SqliteTaskRepository(db_path),
        SqliteRunRepository(db_path),
        SqlitePullRequestRepository(db_path),
        publisher=GitWorkspacePublisher(),
        sink=sink,
        base_branch="main",
        default_branch="main",
    )

    with pytest.raises(ValidatedRevisionMismatchError):
        service.publish(task.task_id, run.run_id)

    tasks, runs, prs = _repos(db_path)
    # No commit was created for the modified revision.
    assert _head(worktree) == head_before
    # No PR lookup or creation happened.
    assert sink.find_calls == 0
    assert sink.create_calls == 0
    assert prs.get_for_run(run.run_id) is None
    # The task remains VALIDATING and the bound revision is unchanged.
    assert tasks.get(task.task_id).status is TaskStatus.VALIDATING
    reloaded = runs.get_run(run.run_id)
    assert reloaded is not None
    assert reloaded.validated_revision == revision
    # The gates persisted by validation are unchanged.
    assert [g.status for g in reloaded.gates] == [QualityGateStatus.PASSED]
    # Nothing was pushed to the remote.
    branches = _git(remote, "branch", "--list", "--format=%(refname:short)").stdout.decode()
    assert branches.strip() == ""


def test_publishable_requires_a_bound_revision(tmp_path: Path) -> None:
    # A run that looks successful but carries no revision identity is refused.
    source, remote, task, run = _setup(tmp_path)
    db_path = str(tmp_path / "factory.db")
    worktree = Path(run.workspace.path)  # type: ignore[union-attr]
    (worktree / "impl.py").write_text("A\n", encoding="utf-8")

    # Simulate a validation that produced green gates durably but never bound a
    # revision (the interrupted case). The run is otherwise SUCCEEDED and green.
    from factory.domain.models import QualityGate

    runs = SqliteRunRepository(db_path)
    ticks = SqliteTaskRepository(db_path)
    stored = runs.get_run(run.run_id)
    assert stored is not None
    stored.status = RunStatus.SUCCEEDED
    stored.gates = (QualityGate("tests", QualityGateStatus.PASSED, required=True),)
    stored.validated_revision = None
    runs.update_run(stored)
    ticks.apply_transition(task.task_id, TaskStatus.RUNNING, TaskStatus.VALIDATING)

    from factory.domain.errors import ValidatedRevisionMissingError

    sink = FakePullRequestSink()
    service = PublicationService(
        SqliteTaskRepository(db_path),
        SqliteRunRepository(db_path),
        SqlitePullRequestRepository(db_path),
        publisher=GitWorkspacePublisher(),
        sink=sink,
        base_branch="main",
        default_branch="main",
    )

    with pytest.raises(ValidatedRevisionMissingError):
        service.publish(task.task_id, run.run_id)

    assert sink.create_calls == 0
    assert _git(remote, "branch", "--list", "--format=%(refname:short)").stdout.decode() == ""
