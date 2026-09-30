"""Security review regressions use disposable Git workspaces and the E1 trace."""

from __future__ import annotations

import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from factory.domain.enums import (
    AgentKind,
    QualityGateStatus,
    RepositoryRole,
    RunStatus,
    TaskStatus,
)
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    QualityGate,
    Repository,
    TaskSource,
    Workspace,
)
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.security import SqliteSecurityReviewGate
from factory.integrations.security.review import GitSecurityInspector, SecurityInspectionError
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.publication import PublicationService, SecurityReviewBlocked
from factory.orchestration.runtime import FactoryRuntime
from tests.fake_adapter import FakeAgentAdapter
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeQualityGateRunner,
    FakeRevisionInspector,
    FakeWorkspaceProvisioner,
    specs,
)
from tests.test_runtime import FakeIssueSource


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def _credential_line(name: str) -> str:
    return name + ' = "' + "abcd" * 6 + '"\n'


@pytest.fixture
def review_parts(tmp_path: Path):
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.invalid")
    (root / "app.py").write_text("answer = 42\n")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    _git(root, "branch", "-M", "main")
    _git(root, "checkout", "-qb", "factory/security-test")
    db = str(tmp_path / "factory.db")
    tasks = SqliteTaskRepository(db)
    runs = SqliteRunRepository(db)
    prs = SqlitePullRequestRepository(db)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = tasks.save(FactoryTask(title="Change app", target_repository="example/target"))
    for before, after in (
        (TaskStatus.DISCOVERED, TaskStatus.READY),
        (TaskStatus.READY, TaskStatus.CLAIMED),
        (TaskStatus.CLAIMED, TaskStatus.RUNNING),
        (TaskStatus.RUNNING, TaskStatus.VALIDATING),
    ):
        tasks.apply_transition(task.task_id, before, after)
    workspace = Workspace(
        repository_slug=task.target_repository,
        branch="factory/security-test",
        path=str(root),
    )
    run = runs.save_run(
        AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.OTHER,
            status=RunStatus.SUCCEEDED,
            workspace=workspace,
            gates=(QualityGate(name="tests", status=QualityGateStatus.PASSED, required=True),),
            validated_revision="reviewed-tree",
        )
    )
    return root, db, task, run, tasks, runs, prs


def test_clean_change_passes_and_records_one_e1_event(review_parts) -> None:
    root, db, task, run, _, _, _ = review_parts
    (root / "app.py").write_text("answer = 43\n")
    gate = SqliteSecurityReviewGate(db, GitSecurityInspector())

    first = gate.review(task, run)
    second = gate.review(task, run)

    assert first == second
    assert not first.critical
    assert [event.name for event in SqliteAuditEventStore(db).for_task(task.task_id)].count(
        "SecurityReviewPassed"
    ) == 1


@pytest.mark.parametrize(
    ("path", "content", "rule"),
    [
        ("app.py", _credential_line("api_key"), "credential"),
        ("app.py", "# ignore " + "previous instructions\n", "prompt-injection"),
        ("Dockerfile", "RUN chmod " + "-R 777 /app\n", "excessive-permission"),
        ("compose.yml", "privileged" + ": true\n", "sandbox-escape"),
        ("auth/settings.py", "verify = " + "false\n", "auth-bypass"),
        ("migrations/drop.sql", "DROP " + "TABLE users;\n", "destructive"),
        (".env.production", "SAFE=placeholder\n", "sensitive-file"),
        ("AGENTS.md", "Agent directions changed\n", "scope-creep"),
    ],
)
def test_risk_patterns_block_before_publication(review_parts, path, content, rule) -> None:
    root, db, task, run, tasks, runs, prs = review_parts
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    gate = SqliteSecurityReviewGate(db, GitSecurityInspector())
    publisher = FakeWorkspacePublisher()
    sink = FakePullRequestSink()
    service = PublicationService(
        tasks, runs, prs, publisher=publisher, sink=sink, security_review=gate
    )

    with pytest.raises(SecurityReviewBlocked):
        service.publish(task.task_id, run.run_id)

    assert publisher.calls == 0
    assert sink.create_calls == 0
    review = gate.review(task, run)
    assert rule in {finding.rule_id for finding in review.findings}
    assert content.strip() not in str(review)
    events = SqliteAuditEventStore(db).for_task(task.task_id)
    assert [event.name for event in events].count("SecurityReviewBlocked") == 1
    evidence = next(event.evidence for event in events if event.name == "SecurityReviewBlocked")
    assert evidence is not None
    assert rule in {item["rule_id"] for item in evidence["findings"]}
    assert content.strip() not in str(evidence)


def test_override_is_explicit_and_revision_bound(review_parts) -> None:
    root, db, task, run, tasks, runs, prs = review_parts
    (root / "app.py").write_text(_credential_line("password"))
    gate = SqliteSecurityReviewGate(db, GitSecurityInspector())
    publisher = FakeWorkspacePublisher()
    service = PublicationService(
        tasks, runs, prs, publisher=publisher, sink=FakePullRequestSink(), security_review=gate
    )
    review = gate.review(task, run)
    assert not gate.is_overridden(task, run, review)
    with pytest.raises(SecurityReviewBlocked):
        service.publish(task.task_id, run.run_id)

    gate.record_override(task, run, review, actor="reviewer", reason="approved for test")
    assert gate.is_overridden(task, run, review)
    assert not gate.is_overridden(task, run, replace(review, revision="different-tree"))
    assert service.publish(task.task_id, run.run_id).task_status is TaskStatus.WAITING_HUMAN
    assert publisher.calls == 1
    assert "SecurityReviewOverridden" in [
        event.name for event in SqliteAuditEventStore(db).for_task(task.task_id)
    ]


def test_unreadable_workspace_fails_closed(review_parts) -> None:
    root, _, task, run, _, _, _ = review_parts
    (root / "app.py").unlink()
    (root / "app.py").symlink_to(root.parent / "outside")
    review = GitSecurityInspector().inspect(task, run)
    assert review.critical
    assert review.findings[0].rule_id == "sandbox-escape"
    run.validated_revision = None
    with pytest.raises(SecurityInspectionError):
        GitSecurityInspector().inspect(task, run)


def test_binary_change_is_unreviewable_and_critical(review_parts) -> None:
    root, _, task, run, _, _, _ = review_parts
    (root / "payload.bin").write_bytes(b"\x00secret")
    review = GitSecurityInspector().inspect(task, run)
    assert "unreviewable-binary" in {finding.rule_id for finding in review.findings}


def test_project_mismatch_fails_closed(review_parts) -> None:
    _, _, task, run, _, _, _ = review_parts
    run.project_id = "other-project"
    with pytest.raises(SecurityInspectionError):
        GitSecurityInspector().inspect(task, run)


def test_committed_change_is_scanned_against_base(review_parts) -> None:
    root, _, task, run, _, _, _ = review_parts
    (root / "app.py").write_text(_credential_line("api_key"))
    _git(root, "add", "app.py")
    _git(root, "commit", "-qm", "agent change")
    review = GitSecurityInspector().inspect(task, run)
    assert "credential" in {finding.rule_id for finding in review.findings}


@pytest.mark.parametrize("risky", [False, True])
def test_runtime_security_review_precedes_waiting_human(tmp_path: Path, risky: bool) -> None:
    db = str(tmp_path / "runtime.db")
    tasks = SqliteTaskRepository(db)
    runs = SqliteRunRepository(db)
    prs = SqlitePullRequestRepository(db)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = FactoryTask(
        title="Review integration",
        target_repository="example/target",
        source=TaskSource("github", "example/control", 78),
    )

    class PreparedGitWorkspace(FakeWorkspaceProvisioner):
        def prepare(self, current_task: FactoryTask, workspace: Workspace) -> Workspace:
            prepared = super().prepare(current_task, workspace)
            root = Path(workspace.path)
            if not (root / ".git").exists():
                _git(root, "init", "-q")
                _git(root, "config", "user.name", "Test")
                _git(root, "config", "user.email", "test@example.invalid")
                (root / "app.py").write_text("answer = 42\n")
                _git(root, "add", ".")
                _git(root, "commit", "-qm", "baseline")
                _git(root, "branch", "-M", "main")
                _git(root, "checkout", "-qb", workspace.branch)
                addition = _credential_line("password") if risky else "answer = 43\n"
                (root / "app.py").write_text(addition)
            return prepared

    publisher = FakeWorkspacePublisher()
    runtime = FactoryRuntime(
        intake=IssueIntakeService(FakeIssueSource(task), tasks),
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=FakeAgentAdapter(kind=AgentKind.OTHER, status=RunStatus.SUCCEEDED),
        provisioner=PreparedGitWorkspace(),
        workspace_root=str(tmp_path / "workspaces"),
        gate_specs=specs("tests"),
        gate_runner=FakeQualityGateRunner(),
        revision_inspector=FakeRevisionInspector(),
        publisher=publisher,
        pull_request_sink=FakePullRequestSink(),
        pull_requests=prs,
        base_branch="main",
        security_review=SqliteSecurityReviewGate(db, GitSecurityInspector()),
        poll_interval=0,
        timeout=1,
    )

    result = runtime.run_once()

    assert result.task_status is (TaskStatus.BLOCKED if risky else TaskStatus.WAITING_HUMAN)
    assert publisher.calls == (0 if risky else 1)
    names = [event.name for event in SqliteAuditEventStore(db).for_task(task.task_id)]
    assert ("SecurityReviewBlocked" if risky else "SecurityReviewPassed") in names
    if not risky:
        assert names.index("SecurityReviewPassed") < names.index("HumanApprovalRequired")
