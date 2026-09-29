"""Private, bounded Codex CLI worker; invoked only by CodexAdapter."""

from __future__ import annotations

import json
import os
import resource
import signal
import subprocess
import sys
import time
from pathlib import Path

MAX_RESULT_BYTES = 16_384
MAX_OUTPUT_BYTES = 1_048_576
_POLL_SECONDS = 0.2


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
                stdout=stdout_file,
                stderr=stderr_file,
                text=True,
                start_new_session=True,
                preexec_fn=_limit_output,
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
            exit_code = process.returncode
            stdout_bytes = stdout_path.stat().st_size
            stderr_bytes = stderr_path.stat().st_size
        if status != "CANCELLED" and process.returncode == 0 and _valid_result(message):
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


def _limit_output() -> None:
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_OUTPUT_BYTES, MAX_OUTPUT_BYTES))


def _valid_result(path: Path) -> bool:
    try:
        if not 0 < path.stat().st_size <= MAX_RESULT_BYTES:
            return False
        return bool(path.read_text(encoding="utf-8").strip())
    except (OSError, UnicodeError):
        return False


if __name__ == "__main__":
    run(sys.argv[1], sys.argv[2], Path(sys.argv[3]), sys.argv[4], float(sys.argv[5]), sys.argv[6])
