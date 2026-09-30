"""Codex CLI adapter tests using a disposable executable and workspace."""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import monotonic, sleep

import pytest

from factory.domain.enums import AgentKind, RunStatus, TaskKind, ValidationOutcome
from factory.domain.models import AgentRun, FactoryTask, QualityGateSpec, Workspace
from factory.integrations.codex import CodexAdapter
from factory.integrations.codex.ecc_skill import SKILL_NAME, load_skill
from factory.integrations.codex.worker import MAX_OUTPUT_BYTES
from factory.integrations.context.skill_source import ApprovedSkillSource
from factory.integrations.gates.local import LocalQualityGateRunner
from factory.orchestration.context import ContextPackBuilder


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


def _dispatch(adapter: CodexAdapter, task: FactoryTask, workspace: Workspace) -> AgentRun:
    pack = ContextPackBuilder((ApprovedSkillSource(adapter._ecc_skill),)).build(task)
    return adapter.dispatch(task, workspace, pack)


def test_pinned_ecc_skill_loading_fails_closed(tmp_path: Path) -> None:
    assert "# Verification Loop Skill" in load_skill(SKILL_NAME)
    with pytest.raises(ValueError, match="unsupported"):
        load_skill("other")
    changed = tmp_path / "SKILL.md"
    changed.write_text(load_skill(SKILL_NAME) + "\nunsafe edit")
    with pytest.raises(ValueError, match="digest mismatch"):
        load_skill(SKILL_NAME, path=changed)
    link = tmp_path / "linked-skill"
    link.symlink_to(changed)
    with pytest.raises(ValueError, match="regular file"):
        load_skill(SKILL_NAME, path=link)
    changed.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        load_skill(SKILL_NAME, path=changed)


@pytest.mark.parametrize(
    ("selection", "task_type", "heading"),
    [
        (SKILL_NAME, None, "# Verification Loop Skill"),
        ("auto", "verification", "# Verification Loop Skill"),
        ("auto", "code-quality", "# Coding Standards & Best Practices"),
        ("auto", "error-handling", "# Error Handling Patterns"),
    ],
)
def test_ecc_codex_then_factory_gate_in_isolated_checkout(
    tmp_path: Path, selection: str, task_type: str | None, heading: str
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    executable = _executable(
        tmp_path,
        f"""import pathlib, sys
instruction = sys.stdin.read()
assert {heading!r} in instruction
assert 'Factory quality gates and human review remain authoritative' in instruction
pathlib.Path('result.txt').write_text('reviewed')
pathlib.Path(sys.argv[7]).write_text('Done')
""",
    )
    workspace = _workspace(checkout)
    adapter = CodexAdapter(executable=executable, ecc_skill=selection)
    task = _task()
    if task_type is not None:
        task.title = f"Implement a feature [type:{task_type}]"
    run = _collect(adapter, _dispatch(adapter, task, workspace))
    assert run.status is RunStatus.SUCCEEDED
    gate = LocalQualityGateRunner().run(
        QualityGateSpec(
            name="artifact",
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('result.txt').exists()",
            ),
        ),
        workspace,
    )
    assert gate.is_green
    failing_gate = LocalQualityGateRunner().run(
        QualityGateSpec(
            name="missing",
            argv=(
                sys.executable,
                "-c",
                "from pathlib import Path; assert Path('missing.txt').exists()",
            ),
        ),
        workspace,
    )
    assert not failing_gate.is_green
    run.gates = (gate, failing_gate)
    assert run.validation_outcome is ValidationOutcome.GATES_FAILED


def test_unavailable_ecc_skill_prevents_dispatch(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="required context"):
        _dispatch(CodexAdapter(ecc_skill="unknown"), _task(), _workspace(tmp_path))


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
assert '--skip-git-repo-check' not in args
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
    run = _dispatch(adapter, _task(), _workspace(checkout))
    assert run.status is RunStatus.RUNNING
    run = _collect(CodexAdapter(environment=environment), run)
    assert run.adapter is AgentKind.CODEX
    assert run.status is RunStatus.SUCCEEDED
    assert run.finished_at is not None
    assert run.summary is None


def test_code_can_write_large_workspace_file_and_last_message(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    executable = _executable(
        tmp_path,
        """import pathlib, sys
args = sys.argv[1:]
pathlib.Path('generated.bin').write_bytes(b'x' * 1_048_577)
pathlib.Path(args[6]).write_text('Done')
print('complete')
""",
    )
    adapter = CodexAdapter(executable=executable)
    run = _collect(adapter, _dispatch(adapter, _task(), _workspace(checkout)))
    assert run.status is RunStatus.SUCCEEDED
    assert (checkout / "generated.bin").stat().st_size == MAX_OUTPUT_BYTES + 1


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_oversized_code_output_succeeds_and_capture_is_bounded(tmp_path: Path, stream: str) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    executable = _executable(
        tmp_path,
        f"""import pathlib, sys
pathlib.Path(sys.argv[7]).write_text('Done')
sys.{stream}.buffer.write(b'x' * {MAX_OUTPUT_BYTES + 1})
sys.{stream}.flush()
""",
    )
    adapter = CodexAdapter(executable=executable)
    workspace = _workspace(checkout)
    run = _collect(adapter, _dispatch(adapter, _task(), workspace))
    evidence = json.loads((CodexAdapter._state_dir(workspace) / f"{run.run_id}.result").read_text())
    assert run.status is RunStatus.SUCCEEDED
    assert evidence["status"] == "SUCCEEDED"
    assert evidence[f"{stream}_bytes"] == MAX_OUTPUT_BYTES


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
    run = _collect(adapter, _dispatch(adapter, _task(), _workspace(checkout)))
    assert run.status is RunStatus.FAILED


@pytest.mark.parametrize("exit_code", [0, 1])
def test_worker_records_actual_exit_code_even_without_message(
    tmp_path: Path, exit_code: int
) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    executable = _executable(
        tmp_path,
        f"import sys\nsys.stderr.write('diagnostic\\n')\nsys.exit({exit_code})\n",
    )
    adapter = CodexAdapter(executable=executable)
    run = _collect(adapter, _dispatch(adapter, _task(), _workspace(checkout)))
    result = CodexAdapter._state_dir(_workspace(checkout)) / f"{run.run_id}.result"
    evidence = json.loads(result.read_text())
    assert run.status is RunStatus.FAILED
    assert evidence == {
        "status": "FAILED",
        "exit_code": exit_code,
        "stdout_bytes": 0,
        "stderr_bytes": len("diagnostic\n"),
        "timed_out": False,
    }


@pytest.mark.parametrize("payload", ["", " ", "x" * 16_385])
def test_malformed_result_fails_closed(tmp_path: Path, payload: str) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    executable = _executable(
        tmp_path,
        f"import pathlib, sys\npathlib.Path(sys.argv[7]).write_text({payload!r})\n",
    )
    adapter = CodexAdapter(executable=executable)
    run = _collect(adapter, _dispatch(adapter, _task(), _workspace(checkout)))
    assert run.status is RunStatus.FAILED


def test_missing_executable_and_timeout_fail_closed(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    workspace = _workspace(checkout)
    missing_adapter = CodexAdapter(executable=str(tmp_path / "missing"))
    missing = _collect(missing_adapter, _dispatch(missing_adapter, _task(), workspace))
    sleeping_adapter = CodexAdapter(
        executable=_executable(tmp_path, "import time; time.sleep(1)\n"), timeout=0.01
    )
    sleeping = _collect(sleeping_adapter, _dispatch(sleeping_adapter, _task(), workspace))
    assert missing.status is RunStatus.FAILED
    assert sleeping.status is RunStatus.FAILED


def test_cancel_stops_an_active_codex_run(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    adapter = CodexAdapter(executable=_executable(tmp_path, "import time; time.sleep(2)\n"))
    run = _dispatch(adapter, _task(), _workspace(checkout))
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


def test_inflight_code_worker_result_from_previous_format_is_accepted(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "checkout")
    state = CodexAdapter._state_dir(workspace)
    state.mkdir()
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.CODEX,
        status=RunStatus.RUNNING,
        workspace=workspace,
        started_at=datetime.now(UTC),
    )
    (state / f"{run.run_id}.result").write_bytes(b"SUCCEEDED")
    assert CodexAdapter().collect(run).status is RunStatus.SUCCEEDED


def test_collect_rejects_other_engine(tmp_path: Path) -> None:
    run = _dispatch(CodexAdapter(executable="missing"), _task(), _workspace(tmp_path))
    run.adapter = AgentKind.OPENHANDS
    with pytest.raises(ValueError):
        CodexAdapter().collect(run)


def test_invalid_workspace_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    run = _dispatch(CodexAdapter(executable="codex"), _task(), _workspace(missing))
    assert run.status is RunStatus.FAILED


def test_code_task_cannot_use_operational_workspace(tmp_path: Path) -> None:
    workspace = Workspace(
        repository_slug="owner/repo", path=str(tmp_path), branch="", kind=TaskKind.OPERATIONAL
    )
    run = _dispatch(CodexAdapter(executable="codex"), _task(), workspace)
    assert run.status is RunStatus.FAILED


def test_environment_excludes_api_keys() -> None:
    environment = {"OPENAI_API_KEY": "secret", "GITHUB_TOKEN": "secret", "HOME": "/home/test"}
    assert CodexAdapter(environment=environment)._env() == {"HOME": "/home/test"}
