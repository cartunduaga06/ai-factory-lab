"""Issue context passes through the real Codex adapter into gated publication."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from factory.domain.enums import RepositoryRole, TaskStatus
from factory.domain.models import FactoryTask, QualityGateSpec, Repository, TaskSource
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.integrations.codex import CodexAdapter
from factory.integrations.context.repository import RepositoryContextSource
from factory.integrations.gates.local import LocalQualityGateRunner
from factory.integrations.openhands.adapter import OpenHandsAdapter
from factory.integrations.openhands.client import OpenHandsClient
from factory.integrations.openhands.execution import OpenHandsExecution
from factory.orchestration.context import ContextPackBuilder
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.runtime import FactoryRuntime
from tests.fake_openhands import FakeTransport
from tests.fake_publish import FakePullRequestSink, FakeWorkspacePublisher
from tests.fake_workspace import (
    FakeRevisionInspector,
    FakeSecurityReviewGate,
    FakeWorkspaceProvisioner,
)
from tests.test_runtime import FakeIssueSource


def test_issue_to_pack_to_codex_gates_and_pr(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "AGENTS.md").write_text("Follow repository rules.")
    (source / "src").mkdir()
    (source / "src" / "feature.py").write_text("FEATURE = 1\n")
    executable = tmp_path / "fake-codex"
    executable.write_text(
        "#!/usr/bin/env python3\n"
        "import pathlib, sys\n"
        "prompt = sys.stdin.read()\n"
        "assert 'Issue 69' in prompt\n"
        "assert 'Follow repository rules.' in prompt\n"
        "assert 'FEATURE = 1' in prompt\n"
        "pathlib.Path('result.txt').write_text('done')\n"
        "pathlib.Path(sys.argv[7]).write_text('Done')\n"
    )
    executable.chmod(0o755)
    db = str(tmp_path / "factory.db")
    tasks, runs, prs = (
        SqliteTaskRepository(db),
        SqliteRunRepository(db),
        SqlitePullRequestRepository(db),
    )
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    task = FactoryTask(
        title="Issue 69",
        body="Implement src/feature.py",
        target_repository="example/target",
        source=TaskSource("github", "example/control", 69),
    )
    sink = FakePullRequestSink()
    publisher = FakeWorkspacePublisher()
    runtime = FactoryRuntime(
        security_review=FakeSecurityReviewGate(),
        intake=IssueIntakeService(FakeIssueSource(task), tasks),
        intake_repository=Repository("example/control", role=RepositoryRole.CONTROL_PLANE),
        tasks=tasks,
        runs=runs,
        adapter=CodexAdapter(executable=str(executable)),
        context_builder=ContextPackBuilder((RepositoryContextSource(str(source)),)),
        provisioner=FakeWorkspaceProvisioner(),
        workspace_root=str(tmp_path / "workspaces"),
        gate_specs=(
            QualityGateSpec(
                "artifact",
                (
                    sys.executable,
                    "-c",
                    "from pathlib import Path; assert Path('result.txt').read_text() == 'done'",
                ),
            ),
        ),
        gate_runner=LocalQualityGateRunner(),
        revision_inspector=FakeRevisionInspector(),
        publisher=publisher,
        pull_request_sink=sink,
        pull_requests=prs,
        base_branch="main",
        poll_interval=0.01,
        timeout=5,
    )
    result = runtime.run_once()
    assert result.task_status is TaskStatus.WAITING_HUMAN
    assert result.pull_request_number is not None
    run = runs.get_run(result.run_id)  # type: ignore[arg-type]
    assert run is not None and run.context_pack is not None
    assert run.gates and run.gates[0].is_green
    assert publisher.calls == 1 and sink.create_calls == 1


def test_both_code_engines_refuse_missing_pack(tmp_path: Path) -> None:
    from factory.domain.models import Workspace

    task = FactoryTask(title="Code", target_repository="example/target")
    workspace = Workspace(
        repository_slug="example/target", branch="factory/test", path=str(tmp_path)
    )
    codex = CodexAdapter()
    openhands = OpenHandsAdapter(
        OpenHandsClient("http://localhost:60000", transport=FakeTransport()),
        OpenHandsExecution(agent_profile_id="profile-1"),
    )
    for adapter in (codex, openhands):
        with pytest.raises(ValueError, match="requires a context pack"):
            adapter.dispatch(task, workspace)
