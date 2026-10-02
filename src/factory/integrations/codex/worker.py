"""Private, bounded Codex CLI worker; invoked only by CodexAdapter."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO, cast

MAX_RESULT_BYTES = 16_384
MAX_OUTPUT_BYTES = 1_048_576
_POLL_SECONDS = 0.2
_READ_BYTES = 65_536


def run(
    executable: str,
    workspace: str,
    state_dir: Path,
    run_id: str,
    timeout: float,
    kind: str,
    model: str = "gpt-6-luna",
    reasoning_effort: str = "low",
) -> None:
    """Execute Codex and atomically record a sanitized terminal status."""
    prompt = state_dir / f"{run_id}.prompt"
    message = state_dir / f"{run_id}.message"
    cancel = state_dir / f"{run_id}.cancel"
    result = state_dir / f"{run_id}.result"
    liveness = state_dir / f"{run_id}.alive"
    stdout_path = state_dir / f"{run_id}.stdout"
    stderr_path = state_dir / f"{run_id}.stderr"
    heartbeat_stop = threading.Event()
    heartbeat: threading.Thread | None = None
    process: subprocess.Popen[bytes] | None = None
    status = "FAILED"
    exit_code: int | None = None
    stdout_bytes = 0
    stderr_bytes = 0
    timed_out = False
    output_exceeded = False
    _write_liveness(liveness)
    try:
        if kind not in ("CODE", "OPERATIONAL"):
            raise ValueError("invalid Codex task kind")

        # cancel() may run after dispatch but before this worker is scheduled.
        # Consume that request before starting any untrusted process.
        if cancel.exists():
            status = "CANCELLED"
        else:
            instruction = prompt.read_text(encoding="utf-8")
            prompt.unlink()
            command = [
                executable,
                "exec",
                "--ignore-user-config",
                "--model",
                model,
                "-c",
                f'model_reasoning_effort="{reasoning_effort}"',
                "--sandbox",
                "workspace-write",
                "--cd",
                workspace,
            ]
            if kind == "OPERATIONAL":
                command.append("--skip-git-repo-check")
            command.extend(("--output-last-message", str(message), "-"))

            with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:
                if cancel.exists():
                    status = "CANCELLED"
                else:
                    with subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                        command,
                        cwd=workspace,
                        env={
                            key: value for key, value in os.environ.items() if key != "PYTHONPATH"
                        },
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        start_new_session=True,
                    ) as process:
                        try:
                            _write_liveness(liveness, process.pid)
                            heartbeat = threading.Thread(
                                target=_heartbeat_loop,
                                args=(liveness, process.pid, heartbeat_stop),
                                daemon=True,
                            )
                            heartbeat.start()
                            status, output_exceeded, timed_out = _drain_output(
                                process,
                                instruction.encode("utf-8"),
                                stdout_file,
                                stderr_file,
                                cancel,
                                timeout,
                            )
                        finally:
                            if process.poll() is None:
                                os.killpg(process.pid, signal.SIGKILL)
                            process.wait()
                    exit_code = process.returncode
                    stdout_file.flush()
                    stderr_file.flush()
                    stdout_bytes = stdout_path.stat().st_size
                    stderr_bytes = stderr_path.stat().st_size

            # A marker observed while the child was exiting wins over its exit
            # status. This closes the final drain-vs-cancel race.
            if cancel.exists():
                status = "CANCELLED"

        if (
            status != "CANCELLED"
            and not timed_out
            and (kind == "CODE" or not output_exceeded)
            and process is not None
            and process.returncode == 0
            and _valid_result(message)
        ):
            status = "SUCCEEDED"
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        pass
    finally:
        if heartbeat is not None:
            heartbeat_stop.set()
            heartbeat.join(timeout=1)
        liveness.unlink(missing_ok=True)
        prompt.unlink(missing_ok=True)
        message.unlink(missing_ok=True)
        stdout_path.unlink(missing_ok=True)
        stderr_path.unlink(missing_ok=True)
        temporary = state_dir / f"{run_id}.result.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "status": status,
                    "exit_code": exit_code,
                    "stdout_bytes": stdout_bytes,
                    "stderr_bytes": stderr_bytes,
                    "timed_out": timed_out,
                }
            ),
            encoding="ascii",
        )
        os.replace(temporary, result)
        # Publish terminal evidence before consuming cancellation. A concurrent
        # cancel() will then observe the result and avoid leaving a stale marker.
        cancel.unlink(missing_ok=True)


def _write_liveness(path: Path, codex_pid: int | None = None) -> None:
    """Publish worker-owned process identity while this worker is executing."""
    # A unique temporary file lets the heartbeat and pipe-drain writers publish
    # independently without replacing each other's in-progress file.
    temporary = path.with_name(f"{path.name}.{threading.get_ident()}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "worker_pid": os.getpid(),
                "codex_pid": codex_pid,
                "heartbeat": datetime.now(UTC).isoformat(),
            }
        ),
        encoding="ascii",
    )
    os.replace(temporary, path)


def _heartbeat_loop(path: Path, codex_pid: int, stop: threading.Event) -> None:
    """Refresh worker-owned evidence even while Codex is quiet on its pipes."""
    while not stop.wait(5):
        try:
            _write_liveness(path, codex_pid)
        except OSError:
            return


def _drain_output(
    process: subprocess.Popen[bytes],
    instruction: bytes,
    stdout_file: BinaryIO,
    stderr_file: BinaryIO,
    cancel: Path,
    timeout: float,
) -> tuple[str, bool, bool]:
    """Capture bounded streams and distinguish true deadline expiry from SIGKILL."""
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    status = "FAILED"
    output_exceeded = False
    timed_out = False
    pending = memoryview(instruction)
    deadline = time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        for stream, target in ((process.stdout, stdout_file), (process.stderr, stderr_file)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, target)
        if pending:
            os.set_blocking(process.stdin.fileno(), False)
            selector.register(process.stdin, selectors.EVENT_WRITE)
        else:
            process.stdin.close()
        while selector.get_map() or process.poll() is None:
            _write_liveness(
                cancel.with_name(cancel.name.removesuffix(".cancel") + ".alive"), process.pid
            )
            if cancel.exists():
                status = "CANCELLED"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = process.poll() is None
                break
            events = selector.select(min(_POLL_SECONDS, remaining))
            # Cancellation is checked again after select: readiness and a
            # marker can arrive together, and cancellation must not depend on
            # which order the kernel reports them in.
            if cancel.exists():
                status = "CANCELLED"
                break
            for key, _ in events:
                stream = cast(BinaryIO, key.fileobj)
                if stream is process.stdin:
                    try:
                        written = os.write(stream.fileno(), pending)
                    except BrokenPipeError:
                        written = len(pending)
                    pending = pending[written:]
                    if not pending:
                        selector.unregister(stream)
                        stream.close()
                    continue
                chunk = os.read(stream.fileno(), _READ_BYTES)
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                target = key.data
                allowed = MAX_OUTPUT_BYTES - target.tell()
                if allowed:
                    target.write(chunk[:allowed])
                if len(chunk) > allowed:
                    output_exceeded = True
                    # Keep draining both pipes so logging volume cannot stall
                    # the child or shorten a CODE run.
    return status, output_exceeded, timed_out


def _valid_result(path: Path) -> bool:
    try:
        if not 0 < path.stat().st_size <= MAX_RESULT_BYTES:
            return False
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return False


if __name__ == "__main__":
    run(
        sys.argv[1],
        sys.argv[2],
        Path(sys.argv[3]),
        sys.argv[4],
        float(sys.argv[5]),
        sys.argv[6],
        sys.argv[7],
        sys.argv[8],
    )
