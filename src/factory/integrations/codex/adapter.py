"""Durable, bounded Codex CLI execution in a factory-provisioned workspace."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

from factory.domain.context import ContextPack
from factory.domain.enums import AgentKind, RunStatus, TaskKind
from factory.domain.models import AgentRun, FactoryTask, Workspace
from factory.domain.operational import parse_scratch_artifact
from factory.integrations.base import AgentAdapterBase

DEFAULT_TIMEOUT_SECONDS = 1800.0
_COLLECTION_GRACE_SECONDS = 5.0
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "CODEX_HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "NO_PROXY",
)


class CodexAdapter(AgentAdapterBase):
    """Start one isolated CLI worker per run, then collect its durable result."""

    def __init__(
        self,
        *,
        executable: str = "codex",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        environment: Mapping[str, str] | None = None,
        ecc_skill: str | None = None,
        model: str = "gpt-6-luna",
        reasoning_effort: str = "low",
    ) -> None:
        if timeout <= 0:
            raise ValueError("Codex timeout must be positive")
        if not model.strip():
            raise ValueError("Codex model must not be empty")
        if reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("invalid Codex reasoning effort")
        self._executable = executable
        self._timeout = timeout
        self._environment = environment
        self._ecc_skill = ecc_skill
        self._model = model
        self._reasoning_effort = reasoning_effort

    @property
    def kind(self) -> AgentKind:
        return AgentKind.CODEX

    def dispatch(
        self, task: FactoryTask, workspace: Workspace, context_pack: ContextPack | None = None
    ) -> AgentRun:
        """Start the worker in the exact checkout supplied by Factory.

        The worker owns the bounded CLI process and writes a small result outside
        the checkout. Factory can persist this RUNNING record immediately and
        collect it even after the factory process restarts.
        """
        if task.kind is TaskKind.CODE and context_pack is None:
            raise ValueError("CODE dispatch requires a context pack")
        run = AgentRun(
            task_id=task.task_id,
            adapter=self.kind,
            status=RunStatus.RUNNING,
            workspace=workspace,
            started_at=datetime.now(UTC),
        )
        prompt: Path | None = None
        try:
            checkout = Path(workspace.path).resolve(strict=True)
            if not checkout.is_dir():
                raise OSError("Codex workspace is not a directory")
            if workspace.kind is not task.kind:
                raise ValueError("Codex task and workspace kinds differ")
            state_dir = self._state_dir(workspace)
            state_dir.mkdir(mode=0o700, exist_ok=True)
            if (
                state_dir.is_symlink()
                or not state_dir.is_dir()
                or state_dir.stat().st_mode & 0o077
                or state_dir.stat().st_uid != os.getuid()
            ):
                raise OSError("unsafe Codex state directory")
            # Run ids are unique. Clear only this run's stale marker before the
            # prompt becomes visible to the worker.
            (state_dir / f"{run.run_id}.cancel").unlink(missing_ok=True)
            prompt = state_dir / f"{run.run_id}.prompt"
            descriptor = os.open(prompt, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                if task.kind is TaskKind.OPERATIONAL:
                    if workspace.kind is not TaskKind.OPERATIONAL:
                        raise ValueError("operational workspace required")
                    declaration = parse_scratch_artifact(task)
                    stream.write(
                        "Create exactly one file in the current scratch directory. "
                        "Use the file name and hexadecimal bytes below. Do not read or "
                        "modify any other path. Do not use sudo, network, or Git. "
                        "Do not print file contents or environment values.\n"
                        f"File name: {declaration.name}\n"
                        f"Bytes (hex): {declaration.payload_hex}\n"
                        "Reply only with a short completion status.\n"
                    )
                else:
                    assert context_pack is not None
                    stream.write(context_pack.render())
            environment = self._env()
            # Import the worker from trusted factory code. Python puts its cwd
            # first on sys.path for -m, so never start it in the target checkout.
            factory_source = Path(__file__).resolve().parents[3]
            environment["PYTHONPATH"] = str(factory_source)
            subprocess.Popen(  # noqa: S603 - fixed module and argv, no shell
                [
                    sys.executable,
                    "-m",
                    "factory.integrations.codex.worker",
                    self._executable,
                    str(checkout),
                    str(state_dir),
                    run.run_id,
                    str(self._timeout),
                    task.kind.value,
                    self._model,
                    self._reasoning_effort,
                ],
                cwd=factory_source,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
            if prompt is not None:
                prompt.unlink(missing_ok=True)
            run.status = RunStatus.FAILED
            if task.kind is TaskKind.OPERATIONAL:
                run.summary = "action=scratch_artifact; dispatch_failed"
            run.finished_at = datetime.now(UTC)
        return run

    def collect(self, run: AgentRun) -> AgentRun:
        """Read a worker result; missing or malformed results fail closed."""
        self._require_identity(run)
        if run.is_terminal:
            return run
        workspace = run.workspace
        assert workspace is not None
        result = self._state_dir(workspace) / f"{run.run_id}.result"
        try:
            if result.exists():
                content = result.read_bytes()
                if len(content) > 512:
                    raise ValueError("oversized worker result")
                legacy = {
                    b"SUCCEEDED": RunStatus.SUCCEEDED,
                    b"FAILED": RunStatus.FAILED,
                    b"CANCELLED": RunStatus.CANCELLED,
                }
                if workspace.kind is TaskKind.CODE and content in legacy:
                    run.status = legacy[content]
                    _capture_liveness(run, self._state_dir(workspace))
                    if run.is_terminal:
                        run.finished_at = datetime.now(UTC)
                    return run
                parsed = json.loads(content)
                if not isinstance(parsed, dict):
                    raise ValueError("invalid worker result")
                _capture_liveness(run, self._state_dir(workspace))
                status = parsed.get("status")
                run.status = (
                    {
                        "SUCCEEDED": RunStatus.SUCCEEDED,
                        "FAILED": RunStatus.FAILED,
                        "CANCELLED": RunStatus.CANCELLED,
                    }.get(status, RunStatus.FAILED)
                    if isinstance(status, str)
                    else RunStatus.FAILED
                )
                if workspace.kind is TaskKind.OPERATIONAL:
                    code = parsed.get("exit_code")
                    out = parsed.get("stdout_bytes")
                    err = parsed.get("stderr_bytes")
                    if not (
                        (code is None or type(code) is int)
                        and type(out) is int
                        and type(err) is int
                        and 0 <= out <= 1_048_576
                        and 0 <= err <= 1_048_576
                    ):
                        raise ValueError("invalid worker evidence")
                    run.summary = (
                        f"action=scratch_artifact; exit_code={code}; "
                        f"stdout_bytes={out}; stderr_bytes={err}"
                    )
            elif run.started_at is None or datetime.now(UTC) >= run.started_at + timedelta(
                seconds=self._timeout + _COLLECTION_GRACE_SECONDS
            ):
                run.status = RunStatus.FAILED
            else:
                _capture_liveness(run, self._state_dir(workspace))
        except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
            run.status = RunStatus.FAILED
        if run.is_terminal:
            run.finished_at = datetime.now(UTC)
        return run

    def cancel(self, run: AgentRun) -> None:
        """Ask an active worker to terminate its process group, idempotently."""
        self._require_identity(run)
        if run.is_terminal:
            return
        workspace = run.workspace
        assert workspace is not None
        state_dir = self._state_dir(workspace)
        if (state_dir / f"{run.run_id}.result").exists():
            return
        cancel = state_dir / f"{run.run_id}.cancel"
        try:
            descriptor = os.open(cancel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            return
        os.close(descriptor)

    def read_agent_heartbeat(self, run: AgentRun) -> datetime | None:
        """Read trusted worker liveness without conflating it with supervisor polling."""
        self._require_identity(run)
        assert run.workspace is not None
        _capture_liveness(run, self._state_dir(run.workspace))
        return run.agent_heartbeat

    @staticmethod
    def _state_dir(workspace: Workspace) -> Path:
        # The directory is a sibling of the checkout so run metadata cannot be
        # picked up by quality gates or committed by the workspace publisher.
        return Path(workspace.path).resolve().parent / ".factory-codex-runs"

    def _env(self) -> dict[str, str]:
        # HOME and CODEX_HOME reuse the service user's ChatGPT login. Factory
        # credentials and OPENAI_API_KEY never cross this process boundary.
        source = self._environment if self._environment is not None else os.environ
        return {key: source[key] for key in _ENV_ALLOWLIST if key in source}

    def _require_identity(self, run: AgentRun) -> None:
        if run.adapter is not self.kind or run.workspace is None:
            raise ValueError("Codex run identity is invalid")


def _capture_liveness(run: AgentRun, state_dir: Path) -> None:
    path = state_dir / f"{run.run_id}.alive"
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 512:
        return
    try:
        evidence = json.loads(path.read_text(encoding="ascii"))
        heartbeat = evidence.get("heartbeat") if isinstance(evidence, dict) else None
        if isinstance(heartbeat, str):
            run.agent_heartbeat = datetime.fromisoformat(heartbeat)
    except (OSError, ValueError, UnicodeError, json.JSONDecodeError):
        return


__all__ = ["CodexAdapter", "DEFAULT_TIMEOUT_SECONDS"]
