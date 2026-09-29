"""Private, bounded Codex CLI worker; invoked only by CodexAdapter."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_RESULT_BYTES = 16_384
_POLL_SECONDS = 0.2


def run(executable: str, workspace: str, state_dir: Path, run_id: str, timeout: float) -> None:
    """Execute Codex and atomically record a sanitized terminal status."""
    prompt = state_dir / f"{run_id}.prompt"
    message = state_dir / f"{run_id}.message"
    cancel = state_dir / f"{run_id}.cancel"
    result = state_dir / f"{run_id}.result"
    status = "FAILED"
    try:
        instruction = prompt.read_text(encoding="utf-8")
        prompt.unlink()
        command = [
            executable,
            "exec",
            "--sandbox",
            "workspace-write",
            "--cd",
            workspace,
            "--output-last-message",
            str(message),
            "-",
        ]
        with subprocess.Popen(  # noqa: S603 - fixed argv, no shell
            command,
            cwd=workspace,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        ) as process:
            try:
                deadline = time.monotonic() + timeout
                pending_input: str | None = instruction
                while True:
                    if cancel.exists():
                        status = "CANCELLED"
                        break
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    try:
                        process.communicate(
                            input=pending_input, timeout=min(_POLL_SECONDS, remaining)
                        )
                        break
                    except subprocess.TimeoutExpired:
                        pending_input = None
            finally:
                if process.poll() is None:
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()
        if status != "CANCELLED" and process.returncode == 0 and _valid_result(message):
            status = "SUCCEEDED"
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        pass
    finally:
        prompt.unlink(missing_ok=True)
        message.unlink(missing_ok=True)
        temporary = state_dir / f"{run_id}.result.tmp"
        temporary.write_text(status, encoding="ascii")
        os.replace(temporary, result)


def _valid_result(path: Path) -> bool:
    try:
        if not 0 < path.stat().st_size <= MAX_RESULT_BYTES:
            return False
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return False


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4], float(sys.argv[5]))
