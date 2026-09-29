"""Codex CLI adapter tests using a disposable executable and workspace."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

import pytest

from factory.domain.enums import AgentKind, RunStatus
from factory.domain.models import AgentRun, FactoryTask, Workspace
from factory.integrations.codex import CodexAdapter


def _task() -> FactoryTask:
    return FactoryTask(title="Implement a feature", target_repository="owner/repo", body="Do it")


def _workspace(path: Path) -> Workspace:
    return Workspace(repository_slug="owner/repo", path=str(path), branch="factory/test")


def _executable(tmp_path: Path, body: str) -> str:
    path = tmp_path / "fake-codex"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(0o755)
    return str(path)


def _collect(adapter: CodexAdapter, run: AgentRun) -> AgentRun:
    deadline = monotonic() + 3
    while monotonic() < deadline:
        adapter.collect(run)
        if run.is_terminal:
            return run
        sleep(0.01)
    pytest.fail("Codex worker did not finish")


def test_codex_uses_assigned_workspace_and_authenticated_home(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    shadow = checkout / "factory"
    shadow.mkdir()
    (shadow / "__init__.py").write_text("raise RuntimeError('untrusted checkout import')\n")
    executable = _executable(
        tmp_path,
        """import os, pathlib, sys
args = sys.argv[1:]
assert args[:4] == ['exec', '--sandbox', 'workspace-write', '--cd']
assert args[4] == os.getcwd()
assert args[5] == '--output-last-message' and args[-1] == '-'
assert 'Implement a feature' in sys.stdin.read()
assert os.environ['HOME'] == '/test/chatgpt-home'
assert os.environ['CODEX_HOME'] == '/test/codex-home'
assert 'OPENAI_API_KEY' not in os.environ
pathlib.Path(args[6]).write_text('Done')
""",
    )
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/test/chatgpt-home",
        "CODEX_HOME": "/test/codex-home",
        "OPENAI_API_KEY": "must-not-forward",
    }
    adapter = CodexAdapter(executable=executable, environment=environment)
    run = adapter.dispatch(_task(), _workspace(checkout))
    assert run.status is RunStatus.RUNNING
    run = _collect(CodexAdapter(environment=environment), run)
    assert run.adapter is AgentKind.CODEX
    assert run.status is RunStatus.SUCCEEDED
    assert run.finished_at is not None
    assert run.summary is None


@pytest.mark.parametrize(
    "body",
    [
        "import sys; sys.exit(1)\n",
        "pass\n",
        "import sys; sys.exit(7)\n",
    ],
)
def test_exit_or_missing_result_fails(tmp_path: Path, body: str) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    adapter = CodexAdapter(executable=_executable(tmp_path, body))
    run = _collect(adapter, adapter.dispatch(_task(), _workspace(checkout)))
    assert run.status is RunStatus.FAILED


@pytest.mark.parametrize("payload", ["", " ", "x" * 16_385])
def test_malformed_result_fails_closed(tmp_path: Path, payload: str) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    executable = _executable(
        tmp_path,
        f"import pathlib, sys\npathlib.Path(sys.argv[7]).write_text({payload!r})\n",
    )
    adapter = CodexAdapter(executable=executable)
    run = _collect(adapter, adapter.dispatch(_task(), _workspace(checkout)))
    assert run.status is RunStatus.FAILED


def test_missing_executable_and_timeout_fail_closed(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    workspace = _workspace(checkout)
    missing_adapter = CodexAdapter(executable=str(tmp_path / "missing"))
    missing = _collect(missing_adapter, missing_adapter.dispatch(_task(), workspace))
    sleeping_adapter = CodexAdapter(
        executable=_executable(tmp_path, "import time; time.sleep(1)\n"), timeout=0.01
    )
    sleeping = _collect(sleeping_adapter, sleeping_adapter.dispatch(_task(), workspace))
    assert missing.status is RunStatus.FAILED
    assert sleeping.status is RunStatus.FAILED


def test_cancel_stops_an_active_codex_run(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    adapter = CodexAdapter(executable=_executable(tmp_path, "import time; time.sleep(2)\n"))
    run = adapter.dispatch(_task(), _workspace(checkout))
    adapter.cancel(run)
    assert _collect(adapter, run).status is RunStatus.CANCELLED


def test_missing_or_malformed_worker_result_fails_closed(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "checkout")
    state = CodexAdapter._state_dir(workspace)
    state.mkdir()
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.CODEX,
        status=RunStatus.RUNNING,
        workspace=workspace,
        started_at=datetime.now(UTC) - timedelta(seconds=20),
    )
    assert CodexAdapter(timeout=1).collect(run).status is RunStatus.FAILED
    run.status = RunStatus.RUNNING
    (state / f"{run.run_id}.result").write_text("UNKNOWN")
    assert CodexAdapter().collect(run).status is RunStatus.FAILED


def test_collect_rejects_other_engine(tmp_path: Path) -> None:
    run = CodexAdapter(executable="missing").dispatch(_task(), _workspace(tmp_path))
    run.adapter = AgentKind.OPENHANDS
    with pytest.raises(ValueError):
        CodexAdapter().collect(run)


def test_invalid_workspace_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    run = CodexAdapter(executable="codex").dispatch(_task(), _workspace(missing))
    assert run.status is RunStatus.FAILED


def test_environment_excludes_api_keys() -> None:
    environment = {"OPENAI_API_KEY": "secret", "GITHUB_TOKEN": "secret", "HOME": "/home/test"}
    assert CodexAdapter(environment=environment)._env() == {"HOME": "/home/test"}
