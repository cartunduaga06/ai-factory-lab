"""Codex worker output limits using a disposable CLI executable."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Timer
from time import monotonic

import pytest

from factory.integrations.codex import worker


def test_liveness_writers_use_independent_atomic_temporaries(tmp_path: Path) -> None:
    """Concurrent publishers cannot unlink the other's in-progress temporary."""
    path = tmp_path / "run.alive"
    ready = threading.Barrier(2)
    write = worker._write_liveness
    errors: list[BaseException] = []
    original = Path.write_text

    def paused_write(target: Path, *args: object, **kwargs: object) -> int:
        ready.wait(timeout=2)
        return original(target, *args, **kwargs)  # type: ignore[arg-type]

    Path.write_text = paused_write  # type: ignore[method-assign]
    try:
        with ThreadPoolExecutor(max_workers=2) as writers:
            futures = [writers.submit(write, path, 101 + index) for index in range(2)]
            for future in futures:
                try:
                    future.result(timeout=3)
                except BaseException as exc:  # surfaced from the writer thread
                    errors.append(exc)
    finally:
        Path.write_text = original  # type: ignore[method-assign]

    assert errors == []
    assert json.loads(path.read_text(encoding="ascii"))["codex_pid"] in {
        101,
        102,
    }


def _execute(
    tmp_path: Path,
    body: str,
    *,
    kind: str = "CODE",
    timeout: float = 3,
    cancel_after: float | None = None,
) -> dict[str, object]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (state / "test.prompt").write_text("Do the task", encoding="utf-8")
    executable = tmp_path / "fake-codex"
    executable.write_text("#!/usr/bin/env python3\n" + body, encoding="utf-8")
    executable.chmod(0o755)
    timer = Timer(cancel_after, lambda: (state / "test.cancel").touch()) if cancel_after else None
    if timer:
        timer.start()
    try:
        worker.run(str(executable), str(workspace), state, "test", timeout, kind)
    finally:
        if timer:
            timer.cancel()
            timer.join()
    assert not (state / "test.stdout").exists()
    assert not (state / "test.stderr").exists()
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
        "timed_out": False,
    }
    assert (tmp_path / "workspace" / "large-artifact").stat().st_size == 2_000_000


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_code_output_over_cap_is_drained_and_succeeds(tmp_path: Path, stream: str) -> None:
    descriptor = 1 if stream == "stdout" else 2
    result = _execute(
        tmp_path,
        f"""import os, pathlib, sys
pathlib.Path(sys.argv[-2]).write_text('Done')
for _ in range(40):
    os.write({descriptor}, b'x' * 65536)
""",
    )
    assert result["status"] == "SUCCEEDED"
    assert result["exit_code"] == 0
    assert result[f"{stream}_bytes"] == worker.MAX_OUTPUT_BYTES


def test_mixed_streams_over_cap_are_drained_independently(tmp_path: Path) -> None:
    result = _execute(
        tmp_path,
        """import os, pathlib, sys
pathlib.Path(sys.argv[-2]).write_text('Done')
for _ in range(20):
    os.write(1, b'o' * 65536)
    os.write(2, b'e' * 65536)
""",
    )
    assert result == {
        "status": "SUCCEEDED",
        "exit_code": 0,
        "stdout_bytes": worker.MAX_OUTPUT_BYTES,
        "stderr_bytes": worker.MAX_OUTPUT_BYTES,
        "timed_out": False,
    }


def test_operational_output_over_cap_fails_after_draining(tmp_path: Path) -> None:
    result = _execute(
        tmp_path,
        f"""import os, pathlib, sys
pathlib.Path(sys.argv[-2]).write_text('Done')
os.write(2, b'x' * ({worker.MAX_OUTPUT_BYTES} + 1))
""",
        kind="OPERATIONAL",
    )
    assert result["status"] == "FAILED"
    assert result["exit_code"] == 0
    assert result["stderr_bytes"] == worker.MAX_OUTPUT_BYTES


@pytest.mark.parametrize("cancel_after", [None, 0.1])
def test_noisy_run_still_honors_timeout_and_cancel(
    tmp_path: Path, cancel_after: float | None
) -> None:
    started = monotonic()
    result = _execute(
        tmp_path,
        f"""import os, pathlib, sys, time
pathlib.Path(sys.argv[-2]).write_text('Done')
os.write(2, b'x' * ({worker.MAX_OUTPUT_BYTES} + 1))
time.sleep(10)
""",
        timeout=0.2 if cancel_after is None else 3,
        cancel_after=cancel_after,
    )
    assert monotonic() - started < 2
    assert result["status"] == ("CANCELLED" if cancel_after else "FAILED")
    assert result["exit_code"] == -9
    assert result["timed_out"] is (cancel_after is None)
    assert result["stderr_bytes"] == worker.MAX_OUTPUT_BYTES


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


def test_external_sigkill_is_not_mistaken_for_worker_timeout(tmp_path: Path) -> None:
    result = _execute(
        tmp_path,
        """import os, signal
os.kill(os.getpid(), signal.SIGKILL)
""",
    )
    assert result["status"] == "FAILED"
    assert result["exit_code"] == -9
    assert result["timed_out"] is False
