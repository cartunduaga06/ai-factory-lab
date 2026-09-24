"""Tests for the concrete local quality gate runner.

Gates run as real local processes with no shell. The workspace is a temporary
directory created by the test, so nothing outside ``tmp_path`` is touched.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from factory.domain.enums import QualityGateStatus
from factory.domain.models import QualityGateSpec, Workspace
from factory.integrations.gates.local import LocalQualityGateRunner

# The interpreter running the tests, so gate commands are portable.
PYTHON = sys.executable


def _workspace(path: Path) -> Workspace:
    path.mkdir(parents=True, exist_ok=True)
    return Workspace(
        repository_slug="example/target",
        branch="factory/t/ws",
        path=str(path),
    )


def _runner(timeout: float = 30.0) -> LocalQualityGateRunner:
    return LocalQualityGateRunner(timeout=timeout)


# -- outcomes --------------------------------------------------------------


def test_passing_gate_is_passed(tmp_path: Path) -> None:
    gate = _runner().run(
        QualityGateSpec(name="tests", argv=(PYTHON, "-c", "raise SystemExit(0)")),
        _workspace(tmp_path / "ws"),
    )
    assert gate.status is QualityGateStatus.PASSED
    assert gate.detail == "exit_code=0"


def test_failing_gate_is_failed(tmp_path: Path) -> None:
    gate = _runner().run(
        QualityGateSpec(name="tests", argv=(PYTHON, "-c", "raise SystemExit(3)")),
        _workspace(tmp_path / "ws"),
    )
    assert gate.status is QualityGateStatus.FAILED
    assert gate.detail == "exit_code=3"


def test_timeout_is_failed(tmp_path: Path) -> None:
    gate = LocalQualityGateRunner(timeout=0.5).run(
        QualityGateSpec(name="slow", argv=(PYTHON, "-c", "import time; time.sleep(10)")),
        _workspace(tmp_path / "ws"),
    )
    assert gate.status is QualityGateStatus.FAILED
    assert gate.detail == "timeout"


def test_missing_command_is_a_sanitized_failure(tmp_path: Path) -> None:
    gate = _runner().run(
        QualityGateSpec(name="missing", argv=("definitely-not-a-real-binary-xyz",)),
        _workspace(tmp_path / "ws"),
    )
    assert gate.status is QualityGateStatus.FAILED
    assert gate.detail == "spawn_error"


def test_optional_gate_keeps_its_required_flag(tmp_path: Path) -> None:
    gate = _runner().run(
        QualityGateSpec(
            name="optional", argv=(PYTHON, "-c", "raise SystemExit(1)"), required=False
        ),
        _workspace(tmp_path / "ws"),
    )
    assert gate.required is False
    assert gate.status is QualityGateStatus.FAILED


# -- isolation and security ------------------------------------------------


def test_gate_runs_in_the_workspace_directory(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "ws")
    gate = _runner().run(
        QualityGateSpec(name="cwd", argv=(PYTHON, "-c", "import os; raise SystemExit(0)")),
        workspace,
    )
    assert gate.status is QualityGateStatus.PASSED

    # Prove the working directory explicitly: the command writes where it ran.
    _runner().run(
        QualityGateSpec(
            name="write",
            argv=(PYTHON, "-c", "open('ran-here.txt','w').write('ok')"),
        ),
        workspace,
    )
    assert (Path(workspace.path) / "ran-here.txt").read_text(encoding="utf-8") == "ok"


def test_raw_stdout_and_stderr_are_not_persisted(tmp_path: Path) -> None:
    secret = "ghp_supersecrettokenmustnotleak"
    gate = _runner().run(
        QualityGateSpec(
            name="noisy",
            argv=(
                PYTHON,
                "-c",
                f"import sys; print('{secret}'); print('{secret}', file=sys.stderr)",
            ),
        ),
        _workspace(tmp_path / "ws"),
    )
    # The command succeeded, but its output must not reach the gate detail.
    assert gate.status is QualityGateStatus.PASSED
    assert gate.detail == "exit_code=0"
    assert secret not in (gate.detail or "")
    assert "supersecret" not in (gate.detail or "")


def test_no_shell_metacharacters_are_interpreted(tmp_path: Path) -> None:
    # If a shell were used, ``;`` would run a second command and the marker file
    # would appear. With argv + shell=False the whole string is a single arg.
    workspace = _workspace(tmp_path / "ws")
    spec = QualityGateSpec(
        name="no-shell",
        argv=(PYTHON, "-c", "import sys; raise SystemExit(0)", "; touch injected.txt"),
    )
    _runner().run(spec, workspace)
    assert not (Path(workspace.path) / "injected.txt").exists()


@pytest.mark.parametrize("attempt", range(3))
def test_gate_runner_is_repeatable(tmp_path: Path, attempt: int) -> None:
    del attempt
    gate = _runner().run(
        QualityGateSpec(name="tests", argv=(PYTHON, "-c", "raise SystemExit(0)")),
        _workspace(tmp_path / "ws"),
    )
    assert gate.status is QualityGateStatus.PASSED
