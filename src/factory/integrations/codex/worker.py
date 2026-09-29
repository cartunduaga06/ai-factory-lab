"""Private, bounded Codex CLI worker; invoked only by CodexAdapter."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import BinaryIO, cast

MAX_RESULT_BYTES = 16_384
MAX_OUTPUT_BYTES = 1_048_576
_POLL_SECONDS = 0.2
_READ_BYTES = 65_536


def run(
    executable: str, workspace: str, state_dir: Path, run_id: str, timeout: float, kind: str
) -> None:
    """Execute Codex and atomically record a sanitized terminal status."""
    prompt = state_dir / f"{run_id}.prompt"
    message = state_dir / f"{run_id}.message"
    cancel = state_dir / f"{run_id}.cancel"
    result = state_dir / f"{run_id}.result"
    stdout_path = state_dir / f"{run_id}.stdout"
    stderr_path = state_dir / f"{run_id}.stderr"
    status = "FAILED"
    exit_code: int | None = None
    stdout_bytes = 0
    stderr_bytes = 0
    try:
        if kind not in ("CODE", "OPERATIONAL"):
            raise ValueError("invalid Codex task kind")
        instruction = prompt.read_text(encoding="utf-8")
        prompt.unlink()
        command = [
            executable,
            "exec",
            "--sandbox",
            "workspace-write",
            "--cd",
            workspace,
        ]
        if kind == "OPERATIONAL":
            command.append("--skip-git-repo-check")
        command.extend(("--output-last-message", str(message), "-"))
        with stdout_path.open("xb") as stdout_file, stderr_path.open("xb") as stderr_file:
            with subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                command,
                cwd=workspace,
                env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            ) as process:
                try:
                    status, output_exceeded = _drain_output(
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
        if (
            status != "CANCELLED"
            and (kind == "CODE" or not output_exceeded)
            and process.returncode == 0
            and _valid_result(message)
        ):
            status = "SUCCEEDED"
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        pass
    finally:
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
                }
            ),
            encoding="ascii",
        )
        os.replace(temporary, result)


def _drain_output(
    process: subprocess.Popen[bytes],
    instruction: bytes,
    stdout_file: BinaryIO,
    stderr_file: BinaryIO,
    cancel: Path,
    timeout: float,
) -> tuple[str, bool]:
    """Capture bounded streams while feeding stdin and watching the run deadline."""
    assert process.stdin is not None and process.stdout is not None and process.stderr is not None
    status = "FAILED"
    output_exceeded = False
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
            if cancel.exists():
                status = "CANCELLED"
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            for key, _ in selector.select(min(_POLL_SECONDS, remaining)):
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
    return status, output_exceeded


def _valid_result(path: Path) -> bool:
    try:
        if not 0 < path.stat().st_size <= MAX_RESULT_BYTES:
            return False
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return False


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4], float(sys.argv[5]), sys.argv[6])
