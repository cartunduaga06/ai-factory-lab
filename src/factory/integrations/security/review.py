"""Bounded, local review of added code with versioned Factory-owned rules."""

from __future__ import annotations

# ruff: noqa: E501 - Each rule expression is kept on one line for review.
import difflib
import hashlib
import os
import re
import subprocess
import time
from pathlib import Path

from factory.domain.models import AgentRun, FactoryTask
from factory.domain.ports import SecurityInspector
from factory.domain.projects import ProjectRegistry, ProjectRoutingError
from factory.domain.security import SECURITY_RULE_VERSION, SecurityFinding, SecurityReview

MAX_FILES = 2000
MAX_BYTES = 4_000_000
MAX_LINES = 20_000
MAX_SECONDS = 120.0
_ENV_KEYS = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "SYSTEMROOT")
_SENSITIVE = re.compile(r"(?:^|/)(?:\.env(?:\.|$)|id_rsa$|id_ed25519$|.*\.(?:pem|key|p12)$)", re.I)
_AGENT_CONTROL = re.compile(
    r"(?:^|/)(?:AGENTS\.md|CLAUDE\.md|\.claude/[^/]+|\.codex/[^/]+|\.github/workflows/[^/]+)$",
    re.I,
)
_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "credential",
        re.compile(
            r"(?i)(?:api[_-]?key|secret|password|token|private[_-]?key)\s*[:=]\s*['\"]?[A-Za-z0-9_+/=-]{16,}"
        ),
    ),
    ("private-key", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")),
    (
        "destructive",
        re.compile(
            r"\b(?:rm\s+-rf\s+/(?:\s|$)|DROP\s+(?:DATABASE|TABLE)|docker\s+system\s+prune\s+-a)\b",
            re.I,
        ),
    ),
    (
        "sandbox-escape",
        re.compile(
            r"(?:privileged\s*:\s*true|--"
            r"privileged|/var/run/docker\.sock|host(?:PID|Network|IPC)\s*:\s*true|security_opt\s*:\s*.*unconfined)",
            re.I,
        ),
    ),
    (
        "excessive-permission",
        re.compile(
            r"(?:chmod\s+(?:-R\s+)?777\b|\b(?:read|write|admin):\s*['\"]?\*['\"]?|\bpermissions\s*:\s*write-all\b)",
            re.I,
        ),
    ),
    (
        "prompt-injection",
        re.compile(
            r"(?i)(?:ignore (?:all )?(?:previous|prior|system) instructions|override (?:system|developer) instructions|disable (?:security|safety) (?:checks|review))"
        ),
    ),
    (
        "auth-bypass",
        re.compile(
            r"(?i)(?:verify\s*=\s*false|ssl_verify\s*:\s*false|allow_anonymous\s*:\s*true|auth(?:entication)?_disabled\s*:\s*true)"
        ),
    ),
)
_SPECIAL = re.compile(
    r"(?:^|/)(?:Dockerfile|docker-compose[^/]*|compose\.ya?ml|\.github/workflows/[^/]+|n8n/[^/]+|infra/[^/]+|auth/[^/]+|migrations/[^/]+|database/[^/]+)$",
    re.I,
)


class SecurityInspectionError(Exception):
    """Review could not verify the exact publishable workspace state."""


class GitSecurityInspector(SecurityInspector):
    """Scan changed lines only; never retain source text in findings."""

    def __init__(self, *, base_ref: str = "main", registry: ProjectRegistry | None = None) -> None:
        self._base_ref = base_ref
        self._registry = registry

    def inspect(self, task: FactoryTask, run: AgentRun) -> SecurityReview:
        workspace = run.workspace
        if workspace is None or not run.validated_revision:
            raise SecurityInspectionError("missing validated workspace")
        if run.project_id != task.project_id or workspace.repository_slug != task.target_repository:
            raise SecurityInspectionError("project identity mismatch")
        root = Path(workspace.path)
        deadline = time.monotonic() + MAX_SECONDS
        if not root.is_dir() or not (root / ".git").exists():
            raise SecurityInspectionError("workspace unavailable")
        try:
            base_ref = (
                self._registry.resolve(task.project_id, task.target_repository).base_ref
                if self._registry is not None
                else self._base_ref
            )
        except ProjectRoutingError:
            raise SecurityInspectionError("project identity mismatch") from None
        if base_ref.startswith("-") or not base_ref.strip():
            raise SecurityInspectionError("invalid base ref")
        base_commit = self._git(root, "rev-parse", "--verify", f"{base_ref}^{{commit}}")
        if len(base_commit.strip()) not in {40, 64}:
            raise SecurityInspectionError("base revision unavailable")
        base = base_commit.decode("ascii").strip()
        changed = self._git(root, "diff", base, "--name-only", "-z", "--")
        untracked = self._git(root, "ls-files", "--others", "--exclude-standard", "-z")
        paths = sorted(set(name for name in (changed + untracked).split(b"\0") if name))
        if len(paths) > MAX_FILES:
            raise SecurityInspectionError("review file limit exceeded")
        findings: list[SecurityFinding] = []
        inspected = 0
        for raw in paths:
            if time.monotonic() >= deadline:
                raise SecurityInspectionError("review time limit exceeded")
            try:
                relative = raw.decode("utf-8", "strict")
            except UnicodeDecodeError:
                raise SecurityInspectionError("invalid path encoding") from None
            path = root / relative
            if not path.resolve().is_relative_to(root.resolve()):
                findings.append(self._finding("sandbox-escape", relative, 0))
                continue
            if path.is_symlink():
                findings.append(self._finding("sandbox-escape", relative, 0))
                continue
            if not path.exists():
                if _SENSITIVE.search(relative) or _SPECIAL.search(relative):
                    findings.append(self._finding("destructive", relative, 0))
                continue
            if not path.is_file():
                raise SecurityInspectionError("unsupported workspace entry")
            size = path.stat().st_size
            inspected += size
            if inspected > MAX_BYTES:
                raise SecurityInspectionError("review size limit exceeded")
            current = path.read_bytes()
            previous = self._git(
                root,
                "show",
                f"{base}:{relative}",
                missing_ok=True,
                timeout=min(60.0, max(0.1, deadline - time.monotonic())),
            )
            if len(previous) > MAX_BYTES:
                raise SecurityInspectionError("review baseline size limit exceeded")
            if previous == current:
                continue
            if b"\0" in current:
                findings.append(self._finding("unreviewable-binary", relative, 0))
                continue
            if _SENSITIVE.search(relative):
                findings.append(self._finding("sensitive-file", relative, 0))
            if _AGENT_CONTROL.search(relative):
                findings.append(self._finding("scope-creep", relative, 0))
            old_lines = previous.decode("utf-8", "replace").splitlines()
            new_lines = current.decode("utf-8", "replace").splitlines()
            if len(old_lines) + len(new_lines) > MAX_LINES:
                raise SecurityInspectionError("review line limit exceeded")
            for tag, _, _, start, end in difflib.SequenceMatcher(
                None, old_lines, new_lines, autojunk=False
            ).get_opcodes():
                if tag == "equal":
                    continue
                for line_number in range(start, end):
                    line = new_lines[line_number]
                    for rule_id, pattern in _RULES:
                        if pattern.search(line):
                            findings.append(self._finding(rule_id, relative, line_number + 1))
                    if _SPECIAL.search(relative) and re.search(
                        r"(?i)(?:0\.0\.0\.0:|network_mode\s*:\s*host|N8N_BASIC_AUTH_ACTIVE\s*[:=]\s*false|SELECT\s+\*\s+FROM\s+users)",
                        line,
                    ):
                        findings.append(
                            self._finding("sensitive-surface", relative, line_number + 1)
                        )
        return SecurityReview(
            SECURITY_RULE_VERSION,
            f"{run.validated_revision}:{base}",
            tuple(sorted(set(findings), key=lambda f: (f.path_digest, f.line, f.rule_id))),
        )

    @staticmethod
    def _finding(rule_id: str, path: str, line: int) -> SecurityFinding:
        return SecurityFinding(rule_id, hashlib.sha256(path.encode()).hexdigest()[:16], line)

    @staticmethod
    def _git(root: Path, *args: str, missing_ok: bool = False, timeout: float = 60.0) -> bytes:
        env = {key: os.environ[key] for key in _ENV_KEYS if key in os.environ}
        env.update(
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0"
        )
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=root,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            raise SecurityInspectionError("git inspection failed") from None
        if result.returncode != 0:
            if missing_ok and result.returncode == 128:
                return b""
            raise SecurityInspectionError("git inspection failed")
        return result.stdout
