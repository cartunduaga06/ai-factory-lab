"""Local, argv-only quality gate execution.

A gate is a command such as ``pytest`` or ``ruff check .``. The runner executes
it inside the run's workspace and reduces the result to a single
:class:`~factory.domain.models.QualityGate`.

Security properties, all enforced here:

* **No shell.** ``shell=False`` is the default and is never overridden, so
  metacharacters in an argument are data, not syntax. Commands come from
  :class:`~factory.domain.models.QualityGateSpec` as an argv tuple, so there is
  no command string to interpolate or split.
* **Bounded.** Every execution has a timeout; exceeding it is a ``FAILED`` gate,
  never a hung factory.
* **Minimal environment.** Only a small allowlist of variables is forwarded, so
  a factory credential in the ambient environment is not handed to the command.
* **Sanitized result.** ``detail`` carries only something like ``exit_code=0``,
  ``exit_code=1`` or ``timeout``. Raw stdout/stderr are never persisted in MVP
  0.1: process output routinely echoes environment values, paths and tokens, and
  the factory has no way to know which of them is sensitive.
"""

from __future__ import annotations

import os
import subprocess

from factory.domain.enums import QualityGateStatus
from factory.domain.models import QualityGate, QualityGateSpec, Workspace
from factory.domain.ports import QualityGateRunner

#: Default bounds. A gate that runs longer than this is treated as failed.
DEFAULT_TIMEOUT_SECONDS = 300.0

#: Environment variables a build/test tool legitimately needs. Everything else —
#: including any credential the factory process holds — is not forwarded.
_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "SYSTEMROOT",
    "PYTHONPATH",
    "VIRTUAL_ENV",
)


class LocalQualityGateRunner(QualityGateRunner):
    """Runs gates as local subprocesses with no shell and a bounded timeout."""

    def __init__(self, *, timeout: float = DEFAULT_TIMEOUT_SECONDS) -> None:
        self._timeout = timeout

    def __repr__(self) -> str:
        return f"LocalQualityGateRunner(timeout={self._timeout!r})"

    def run(self, spec: QualityGateSpec, workspace: Workspace) -> QualityGate:
        """Execute ``spec`` with ``workspace.path`` as the working directory."""
        try:
            completed = subprocess.run(  # noqa: S603 - argv form, shell is never used
                list(spec.argv),
                cwd=workspace.path,
                env=self._env(),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return self._result(spec, QualityGateStatus.FAILED, "timeout")
        except (OSError, subprocess.SubprocessError):
            # The command could not even be launched. The OS error text can
            # carry the command line, so it is discarded rather than persisted.
            return self._result(spec, QualityGateStatus.FAILED, "spawn_error")

        # Only the numeric exit code is kept; stdout/stderr are dropped.
        status = QualityGateStatus.PASSED if completed.returncode == 0 else QualityGateStatus.FAILED
        return self._result(spec, status, f"exit_code={completed.returncode}")

    @staticmethod
    def _result(spec: QualityGateSpec, status: QualityGateStatus, detail: str) -> QualityGate:
        return QualityGate(
            name=spec.name,
            status=status,
            detail=detail,
            required=spec.required,
        )

    @staticmethod
    def _env() -> dict[str, str]:
        """Minimal, secret-free environment for a gate subprocess."""
        return {key: os.environ[key] for key in _ENV_ALLOWLIST if key in os.environ}


__all__ = ["DEFAULT_TIMEOUT_SECONDS", "LocalQualityGateRunner"]
