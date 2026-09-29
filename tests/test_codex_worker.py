"""Codex worker output limits using a disposable CLI executable."""

from __future__ import annotations

import json
from pathlib import Path
from time import monotonic

import pytest

from factory.integrations.codex import worker


def _execute(tmp_path: Path, body: str, *, kind: str = "CODE") -> dict[str, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "test.prompt").write_text("Do the task", encoding="utf-8")
    executable = tmp_path / "fake-codex"
    executable.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    executable.chmod(0o755)
    worker.run(str(executable), str(workspace), state, "test", 3, kind)
    return json.loads((state / "test.result").read_text(encoding="ascii"))


@pytest.mark.parametrize("kind", ["CODE", "OPERATIONAL"])
def test_workspace_file_larger_than_output_cap_is_allowed(tmp_path: Path, kind: str) -> None:
    result = _execute(
        tmp_path,
        f"""import pathlib, sys
assert ('--skip-git-repo-check' in sys.argv) is {kind == "OPERATIONAL"}
assert sys.stdin.read() == 'Do the task'
pathlib.Path('large-artifact').write_bytes(b'x' * 2_000_000)
pathlib.Path(sys.argv[-2]).write_text('Done')
print('ok')
""",
        kind=kind,
    )
    assert result == {
        "status": "SUCCEEDED",
        "exit_code": 0,
        "stdout_bytes": 3,
        "stderr_bytes": 0,
    }
    assert (tmp_path / "workspace" / "large-artifact").stat().st_size == 2_000_000


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_over_cap_kills_worker_and_fails_closed(tmp_path: Path, stream: str) -> None:
    descriptor = 1 if stream == "stdout" else 2
    started = monotonic()
    result = _execute(
        tmp_path,
        f"""import os, pathlib, sys, time
pathlib.Path(sys.argv[-2]).write_text('Done')
os.write({descriptor}, b'x' * ({worker.MAX_OUTPUT_BYTES} + 1))
time.sleep(10)
""",
    )
    assert monotonic() - started < 3
    assert result["status"] == "FAILED"
    assert result["exit_code"] == -9
    assert result[f"{stream}_bytes"] == worker.MAX_OUTPUT_BYTES


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_output_at_cap_can_succeed(tmp_path: Path, stream: str) -> None:
    descriptor = 1 if stream == "stdout" else 2
    result = _execute(
        tmp_path,
        f"""import os, pathlib, sys
pathlib.Path(sys.argv[-2]).write_text('Done')
os.write({descriptor}, b'x' * {worker.MAX_OUTPUT_BYTES})
""",
    )
    assert result["status"] == "SUCCEEDED"
    assert result[f"{stream}_bytes"] == worker.MAX_OUTPUT_BYTES
