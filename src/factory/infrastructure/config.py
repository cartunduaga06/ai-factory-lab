"""Configuration model for AI Factory Lab.

Configuration is read from environment variables only. Secrets are never
hard-coded, logged or defaulted to real values: a missing credential surfaces
as ``None`` until a component that needs it actually runs. This lets the
repository be cloned and tested with zero integrations configured.

See ``.env.example`` for the full, placeholder-only reference.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlsplit

from factory.domain.models import QualityGateSpec
from factory.domain.projects import ProjectProfile, ProjectRegistry

DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_GITHUB_GIT_HOST = "github.com"
DEFAULT_GITHUB_WRITE_USERNAME = "x-access-token"
DEFAULT_WORKSPACE_ROOT = "./.workspaces"
DEFAULT_OPENHANDS_WORKSPACE_ROOT = "./.workspaces"
DEFAULT_DATABASE_PATH = "./factory.db"
DEFAULT_TARGET_BRANCH = "main"
DEFAULT_RUN_POLL_INTERVAL = 5.0
DEFAULT_RUN_TIMEOUT = 1800.0
DEFAULT_CODEX_MODEL = "gpt-6-luna"
DEFAULT_CODEX_REASONING_EFFORT = "low"
# Idle wait between watch iterations when no eligible work exists. Positive by
# default so an unattended worker cannot spin against the GitHub API.
DEFAULT_WATCH_IDLE_INTERVAL = 60.0
DEFAULT_MAX_CONCURRENCY = 2
DEFAULT_HEARTBEAT_INTERVAL = 180.0
DEFAULT_MISSED_HEARTBEATS = 2


def _max_concurrency(raw: str | None) -> int:
    value = _clean(raw)
    if value is None:
        return DEFAULT_MAX_CONCURRENCY
    if value not in {"1", "2"}:
        raise ValueError("FACTORY_MAX_CONCURRENCY must be 1 or 2")
    return int(value)


class DatabaseScheme(StrEnum):
    """Supported persistence backends."""

    SQLITE = "sqlite"


class UnsupportedDatabaseError(ValueError):
    """Raised when ``DATABASE_URL`` names a scheme the factory cannot use yet."""


class InvalidGateSpecError(ValueError):
    """A configured quality gate specification could not be parsed.

    The message names only the environment variable and the offending gate, never
    a value it could not parse — configuration may be logged.
    """


def _parse_database_targets(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse the operator-owned SQLite allowlist without reflecting paths in errors."""
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("FACTORY_DATABASE_READONLY_TARGETS is invalid") from None
    if (
        not isinstance(value, dict)
        or len(value) > 16
        or any(
            not isinstance(key, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key) is None
            or key in {".", ".."}
            or not isinstance(path, str)
            or not path.startswith("/")
            or "\x00" in path
            for key, path in value.items()
        )
    ):
        raise ValueError("FACTORY_DATABASE_READONLY_TARGETS is invalid")
    return tuple(sorted(value.items()))


def _parse_service_health_targets(raw: str | None) -> tuple[tuple[str, str], ...]:
    """Parse exact HTTPS health probe URLs from operator-owned configuration."""
    if not raw:
        return ()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError("FACTORY_SERVICE_HEALTH_TARGETS is invalid") from None
    if not isinstance(value, dict) or len(value) > 16:
        raise ValueError("FACTORY_SERVICE_HEALTH_TARGETS is invalid")
    targets: list[tuple[str, str]] = []
    for key, url in value.items():
        if (
            not isinstance(key, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", key) is None
            or key in {".", ".."}
            or not isinstance(url, str)
        ):
            raise ValueError("FACTORY_SERVICE_HEALTH_TARGETS is invalid")
        parsed = urlsplit(url)
        try:
            port = parsed.port
        except ValueError:
            raise ValueError("FACTORY_SERVICE_HEALTH_TARGETS is invalid") from None
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
        except ValueError:
            address = None
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname.lower() in {"localhost"}
            or parsed.hostname.lower().endswith(".localhost")
            or parsed.hostname.lower().endswith(".local")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port not in (None, 443)
            or "\\" in url
            or parsed.path == ""
            or (address is not None and not address.is_global)
            or any(ord(char) < 32 or ord(char) == 127 for char in url)
        ):
            raise ValueError("FACTORY_SERVICE_HEALTH_TARGETS is invalid")
        targets.append((key, url))
    return tuple(sorted(targets))


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class AgentEngine(StrEnum):
    CODEX = "codex"
    OPENHANDS = "openhands"


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


def _redact_url(value: str | None) -> str | None:
    """Mask any ``user:password@`` userinfo inside a URL, keeping the rest.

    A connection string is not a credential field, but it can still carry one,
    so redaction must not assume it is safe to log verbatim.
    """
    if value is None:
        return None
    scheme, sep, remainder = value.partition("://")
    if not sep or "@" not in remainder:
        return value
    _, _, host_part = remainder.partition("@")
    return f"{scheme}://***@{host_part}"


def _clean(value: str | None) -> str | None:
    """Return ``None`` for unset/empty values; otherwise the stripped string.

    Also treats unresolved ``<placeholder>`` values from ``.env.example`` as
    unset so an accidentally copied example file does not look like real config.
    """
    if value is None:
        return None
    stripped = value.strip()
    if not stripped or (stripped.startswith("<") and stripped.endswith(">")):
        return None
    return stripped


def _duration(value: str | None, default: float) -> float:
    cleaned = _clean(value)
    if cleaned is None:
        return default
    try:
        parsed = float(cleaned)
    except ValueError:
        return default
    return parsed if math.isfinite(parsed) and parsed >= 0 else default


@dataclass(slots=True, frozen=True)
class GitHubConfig:
    """GitHub is the source of work (Issues) and the target of results (PRs)."""

    token: str | None
    api_url: str
    control_plane_repo: str | None
    target_repo: str | None

    @property
    def enabled(self) -> bool:
        return self.token is not None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> GitHubConfig:
        return cls(
            token=_clean(env.get("GITHUB_TOKEN")),
            api_url=_clean(env.get("GITHUB_API_URL")) or DEFAULT_GITHUB_API_URL,
            control_plane_repo=_clean(env.get("FACTORY_GITHUB_REPO")),
            target_repo=_clean(env.get("FACTORY_TARGET_REPO")),
        )


@dataclass(slots=True, frozen=True)
class AgentConfig:
    """Connection details for a single agent execution engine.

    ``api_key`` is the engine's own execution credential (an LLM key). It is
    distinct from ``session_api_key``: a self-hosted agent server such as
    OpenHands authenticates *requests* with a session key (the
    ``X-Session-API-Key`` header) that is unrelated to any LLM credential.
    Either may be absent — a local server can be unauthenticated, and an agent
    server that holds its own LLM configuration needs no key from the factory.
    """

    api_key: str | None
    base_url: str | None = None
    session_api_key: str | None = None
    agent_profile_id: str | None = None

    @property
    def enabled(self) -> bool:
        """Whether the factory can reach this engine at all.

        A configured ``base_url`` is what the Phase 3 OpenHands adapter needs; an
        ``api_key`` alone does not make the engine dispatchable.
        """
        return self.api_key is not None or self.base_url is not None


@dataclass(slots=True, frozen=True)
class LoggingConfig:
    level: str = "INFO"
    fmt: LogFormat = LogFormat.TEXT

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> LoggingConfig:
        raw_format = (_clean(env.get("FACTORY_LOG_FORMAT")) or LogFormat.TEXT.value).lower()
        try:
            fmt = LogFormat(raw_format)
        except ValueError:
            fmt = LogFormat.TEXT
        return cls(
            level=(_clean(env.get("FACTORY_LOG_LEVEL")) or "INFO").upper(),
            fmt=fmt,
        )


@dataclass(slots=True, frozen=True)
class DatabaseConfig:
    """Parsed persistence target.

    Phase 2A supports SQLite only, but the URL is parsed into a scheme + path
    pair rather than kept opaque so an unsupported backend (for example a future
    ``postgresql://`` target) is rejected with a clear message instead of failing
    deep inside a driver. Both the plain ``sqlite://`` form and the
    ``sqlite+pysqlite://`` form already used in ``.env.example`` are accepted.
    """

    scheme: DatabaseScheme
    path: str

    @classmethod
    def from_url(cls, url: str | None) -> DatabaseConfig:
        if url is None:
            return cls(scheme=DatabaseScheme.SQLITE, path=DEFAULT_DATABASE_PATH)

        scheme_token, sep, remainder = url.partition("://")
        if not sep or not scheme_token:
            raise UnsupportedDatabaseError(
                f"DATABASE_URL must look like '<scheme>://<path>', got {url!r}"
            )

        # SQLAlchemy-style driver suffixes are accepted: sqlite+pysqlite == sqlite.
        normalized = scheme_token.split("+", 1)[0].lower()
        if normalized != DatabaseScheme.SQLITE.value:
            raise UnsupportedDatabaseError(
                f"unsupported database scheme {normalized!r}; Phase 2A supports 'sqlite' only"
            )

        # A triple-slash URL (sqlite:///./factory.db) yields '/./factory.db'; a
        # single leading slash is the URL separator, not part of the path.
        path = remainder[1:] if remainder.startswith("/") else remainder
        if not path:
            raise UnsupportedDatabaseError("DATABASE_URL must name a database file")
        return cls(scheme=DatabaseScheme.SQLITE, path=path)

    @property
    def url(self) -> str:
        return f"{self.scheme.value}:///{self.path}"


@dataclass(slots=True, frozen=True)
class FactoryConfig:
    """Top-level, validated configuration for the whole factory."""

    environment: Environment
    github: GitHubConfig
    openhands: AgentConfig
    codex: AgentConfig
    agent_engine: AgentEngine
    database_url: str | None
    workspace_root: str
    openhands_workspace_root: str
    logging: LoggingConfig
    # Local checkout the workspace provisioner branches from. Injected, never
    # hard-coded: the factory has no business assuming a machine-specific path.
    source_checkout: str | None = None
    # Optional commit-ish a new factory branch starts from (default: source HEAD).
    workspace_base_ref: str | None = None
    # Declarative gate definitions supplied by the application layer. The factory
    # never invents a gate a repository did not define.
    quality_gates: tuple[QualityGateSpec, ...] = ()
    task_quality_gates: Mapping[str, tuple[QualityGateSpec, ...]] = field(default_factory=dict)
    codex_ecc_skill: str | None = None
    codex_model: str = DEFAULT_CODEX_MODEL
    codex_reasoning_effort: str = DEFAULT_CODEX_REASONING_EFFORT
    # Separate WRITE credential for Phase 5 publication (push + PR). Distinct from
    # the read-only intake token: the factory must not implicitly reuse a
    # read-scoped credential for writes.
    github_write_token: str | None = field(default=None, repr=False)
    # Non-secret HTTPS Basic Auth username for the dedicated write credential.
    github_write_username: str = DEFAULT_GITHUB_WRITE_USERNAME
    # Explicit base branch a published PR targets. Defaults to ``main``.
    target_default_branch: str = DEFAULT_TARGET_BRANCH
    # Git host an authenticated HTTPS push may target. Injectable so a GitHub
    # Enterprise deployment does not need code changes. Not a secret.
    github_git_host: str = DEFAULT_GITHUB_GIT_HOST
    run_poll_interval: float = DEFAULT_RUN_POLL_INTERVAL
    run_timeout: float = DEFAULT_RUN_TIMEOUT
    # Idle wait between iterations of the automatic worker. Only ``factory watch``
    # reads it; ``factory run`` stays a single bounded pass.
    watch_idle_interval: float = DEFAULT_WATCH_IDLE_INTERVAL
    max_concurrency: int = DEFAULT_MAX_CONCURRENCY
    heartbeat_interval: float = DEFAULT_HEARTBEAT_INTERVAL
    missed_heartbeats: int = DEFAULT_MISSED_HEARTBEATS
    trello_status_card_id: str | None = None
    trello_key: str | None = field(default=None, repr=False)
    trello_token: str | None = field(default=None, repr=False)
    trello_backlog_list_id: str | None = None
    trello_ready_label_id: str | None = None
    trello_done_list_id: str | None = None
    # Command the OpenHands conversation runs as a shared-workspace hook. When
    # set, it is passed as ``hook_config`` on conversation creation: it
    # normalizes owner-side after ``file_editor`` writes and blocks completion
    # (exit 2) when the shared permission policy cannot be enforced. It is a
    # command local to the OpenHands container, not a factory process argument.
    openhands_shared_workspace_hook_command: str | None = None
    operational_scratch_root: str | None = None
    database_readonly_targets: tuple[tuple[str, str], ...] = ()
    service_health_targets: tuple[tuple[str, str], ...] = ()
    project_registry: ProjectRegistry | None = None

    @property
    def database(self) -> DatabaseConfig:
        """Parsed database target, validated on access.

        Parsing is deferred so that merely loading configuration never raises;
        an unsupported scheme surfaces when a component actually needs storage.
        """
        return DatabaseConfig.from_url(self.database_url)

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FactoryConfig:
        source: Mapping[str, str] = env if env is not None else os.environ
        write_username = (
            _clean(source.get("GITHUB_WRITE_USERNAME")) or DEFAULT_GITHUB_WRITE_USERNAME
        )
        if any(
            char.isspace() or ord(char) < 32 or ord(char) == 127 or char == ":"
            for char in write_username
        ):
            raise ValueError("GITHUB_WRITE_USERNAME contains unsafe characters")
        engine = _clean(source.get("FACTORY_AGENT_ENGINE")) or AgentEngine.CODEX.value
        try:
            agent_engine = AgentEngine(engine)
        except ValueError:
            raise ValueError("FACTORY_AGENT_ENGINE must be codex or openhands") from None
        ecc_skill = _clean(source.get("FACTORY_CODEX_ECC_SKILL"))
        if ecc_skill not in (None, "verification-loop", "auto"):
            raise ValueError("FACTORY_CODEX_ECC_SKILL must be auto, verification-loop or unset")
        codex_model = _clean(source.get("FACTORY_CODEX_MODEL")) or DEFAULT_CODEX_MODEL
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", codex_model) is None:
            raise ValueError("FACTORY_CODEX_MODEL is invalid")
        codex_reasoning_effort = (
            _clean(source.get("FACTORY_CODEX_REASONING_EFFORT")) or DEFAULT_CODEX_REASONING_EFFORT
        ).lower()
        if codex_reasoning_effort not in {"minimal", "low", "medium", "high", "xhigh"}:
            raise ValueError("FACTORY_CODEX_REASONING_EFFORT is invalid")
        raw_env = (_clean(source.get("FACTORY_ENV")) or Environment.DEVELOPMENT.value).lower()
        try:
            environment = Environment(raw_env)
        except ValueError:
            environment = Environment.DEVELOPMENT
        quality_gates = parse_gate_specs(source.get("FACTORY_QUALITY_GATES"))
        raw_projects = _clean(source.get("FACTORY_PROJECTS"))
        project_registry = _parse_projects(raw_projects) if raw_projects else None
        task_quality_gates = parse_task_gate_specs(source.get("FACTORY_TASK_QUALITY_GATES"))
        common_names = {spec.name for spec in quality_gates}
        if any(
            common_names.intersection(spec.name for spec in specs)
            for specs in task_quality_gates.values()
        ):
            raise InvalidGateSpecError("task quality gate duplicates a repository gate")
        return cls(
            environment=environment,
            github=GitHubConfig.from_env(source),
            openhands=AgentConfig(
                api_key=_clean(source.get("OPENHANDS_API_KEY")),
                base_url=_clean(source.get("OPENHANDS_BASE_URL")),
                session_api_key=_clean(source.get("OPENHANDS_SESSION_API_KEY")),
                agent_profile_id=_clean(source.get("OPENHANDS_AGENT_PROFILE_ID")),
            ),
            codex=AgentConfig(api_key=_clean(source.get("CODEX_API_KEY"))),
            agent_engine=agent_engine,
            database_url=_clean(source.get("DATABASE_URL")),
            workspace_root=_clean(source.get("FACTORY_WORKSPACE_ROOT")) or DEFAULT_WORKSPACE_ROOT,
            openhands_workspace_root=(
                _clean(source.get("OPENHANDS_WORKSPACE_ROOT")) or DEFAULT_OPENHANDS_WORKSPACE_ROOT
            ),
            logging=LoggingConfig.from_env(source),
            source_checkout=_clean(source.get("FACTORY_SOURCE_CHECKOUT")),
            workspace_base_ref=_clean(source.get("FACTORY_WORKSPACE_BASE_REF")),
            quality_gates=quality_gates,
            project_registry=project_registry,
            task_quality_gates=task_quality_gates,
            codex_ecc_skill=ecc_skill,
            codex_model=codex_model,
            codex_reasoning_effort=codex_reasoning_effort,
            github_write_token=_clean(source.get("GITHUB_WRITE_TOKEN")),
            github_write_username=write_username,
            target_default_branch=(
                _clean(source.get("FACTORY_TARGET_DEFAULT_BRANCH")) or DEFAULT_TARGET_BRANCH
            ),
            github_git_host=(
                _clean(source.get("FACTORY_GITHUB_GIT_HOST")) or DEFAULT_GITHUB_GIT_HOST
            ),
            run_poll_interval=_duration(
                source.get("FACTORY_RUN_POLL_INTERVAL"), DEFAULT_RUN_POLL_INTERVAL
            ),
            run_timeout=_duration(source.get("FACTORY_RUN_TIMEOUT"), DEFAULT_RUN_TIMEOUT),
            watch_idle_interval=_duration(
                source.get("FACTORY_WATCH_IDLE_INTERVAL"), DEFAULT_WATCH_IDLE_INTERVAL
            ),
            max_concurrency=_max_concurrency(source.get("FACTORY_MAX_CONCURRENCY")),
            heartbeat_interval=max(
                120.0,
                min(
                    300.0,
                    _duration(source.get("FACTORY_HEARTBEAT_INTERVAL"), DEFAULT_HEARTBEAT_INTERVAL),
                ),
            ),
            missed_heartbeats=max(
                1,
                min(
                    1000,
                    int(
                        _duration(
                            source.get("FACTORY_MISSED_HEARTBEATS"), DEFAULT_MISSED_HEARTBEATS
                        )
                    ),
                ),
            ),
            trello_status_card_id=_clean(source.get("FACTORY_TRELLO_STATUS_CARD_ID")),
            trello_key=_clean(source.get("FACTORY_TRELLO_KEY")),
            trello_token=_clean(source.get("FACTORY_TRELLO_TOKEN")),
            trello_backlog_list_id=_clean(source.get("FACTORY_TRELLO_BACKLOG_LIST_ID")),
            trello_ready_label_id=_clean(source.get("FACTORY_TRELLO_READY_LABEL_ID")),
            trello_done_list_id=_clean(source.get("FACTORY_TRELLO_DONE_LIST_ID")),
            openhands_shared_workspace_hook_command=_clean(
                source.get("OPENHANDS_SHARED_WORKSPACE_HOOK_COMMAND")
            ),
            operational_scratch_root=_clean(source.get("FACTORY_OPERATIONAL_SCRATCH_ROOT")),
            database_readonly_targets=_parse_database_targets(
                source.get("FACTORY_DATABASE_READONLY_TARGETS")
            ),
            service_health_targets=_parse_service_health_targets(
                source.get("FACTORY_SERVICE_HEALTH_TARGETS")
            ),
        )

    def redacted(self) -> dict[str, object]:
        """Return a log-safe view with every credential masked.

        Use this instead of ``dataclasses.asdict`` whenever config is logged.
        """
        return {
            "environment": self.environment.value,
            "github": {
                "api_url": self.github.api_url,
                "control_plane_repo": self.github.control_plane_repo,
                "target_repo": self.github.target_repo,
                "token": "***" if self.github.token else None,
            },
            "openhands": {
                "base_url": self.openhands.base_url,
                "agent_profile_id": self.openhands.agent_profile_id,
                "api_key": "***" if self.openhands.api_key else None,
                "session_api_key": "***" if self.openhands.session_api_key else None,
            },
            "codex": {"api_key": "***" if self.codex.api_key else None},
            "agent_engine": self.agent_engine.value,
            "database_url": _redact_url(self.database_url),
            "workspace_root": self.workspace_root,
            "openhands_workspace_root": self.openhands_workspace_root,
            "source_checkout": self.source_checkout,
            "workspace_base_ref": self.workspace_base_ref,
            "quality_gates": [
                {"name": spec.name, "required": spec.required} for spec in self.quality_gates
            ],
            "projects": [
                {
                    "project_id": p.project_id,
                    "repository": p.repository_slug,
                    "source_checkout": p.source_checkout,
                    "base_ref": p.base_ref,
                    "context_profile": p.context_profile,
                    "deploy_policy": p.deploy_policy,
                    "deploy_required": p.deploy_required,
                    "provider_ci_required": p.provider_ci_required,
                    "required_ci_checks": list(p.required_ci_checks),
                    "git_host": p.git_host,
                    "gates": [g.name for g in p.gates],
                }
                for p in self.project_registry.profiles
            ]
            if self.project_registry
            else [],
            "task_quality_gates": {
                ref: [{"name": spec.name, "required": spec.required} for spec in specs]
                for ref, specs in self.task_quality_gates.items()
            },
            "codex_ecc_skill": self.codex_ecc_skill,
            "codex_model": self.codex_model,
            "codex_reasoning_effort": self.codex_reasoning_effort,
            "github_write_token": "***" if self.github_write_token else None,
            "github_write_username": self.github_write_username,
            "target_default_branch": self.target_default_branch,
            "github_git_host": self.github_git_host,
            "run_poll_interval": self.run_poll_interval,
            "run_timeout": self.run_timeout,
            "watch_idle_interval": self.watch_idle_interval,
            "max_concurrency": self.max_concurrency,
            "heartbeat_interval": self.heartbeat_interval,
            "missed_heartbeats": self.missed_heartbeats,
            "trello_status_card_id": self.trello_status_card_id,
            "trello_key": "***" if self.trello_key else None,
            "trello_token": "***" if self.trello_token else None,
            "trello_backlog_list_id": self.trello_backlog_list_id,
            "trello_ready_label_id": self.trello_ready_label_id,
            "trello_done_list_id": self.trello_done_list_id,
            "openhands_shared_workspace_hook_command": (
                self.openhands_shared_workspace_hook_command
            ),
            "operational_scratch_root": self.operational_scratch_root,
            "database_readonly_target_ids": [name for name, _ in self.database_readonly_targets],
            "service_health_target_ids": [name for name, _ in self.service_health_targets],
            "logging": {"level": self.logging.level, "format": self.logging.fmt.value},
        }


def parse_gate_specs(raw: str | None) -> tuple[QualityGateSpec, ...]:
    """Parse the JSON gate definition supplied through ``FACTORY_QUALITY_GATES``.

    The wire format is a JSON array of objects::

        [{"name": "tests", "argv": ["pytest"], "required": true}]

    ``argv`` is a list, never a shell string, so nothing is ever interpolated
    into a shell. Unset or empty configuration yields no gates, which is
    documented behaviour: the factory does not invent gates a repository did not
    define. A malformed definition raises :class:`InvalidGateSpecError` rather
    than being silently ignored, so a typo cannot disable validation.
    """
    cleaned = _clean(raw)
    if cleaned is None:
        return ()
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError:
        raise InvalidGateSpecError("FACTORY_QUALITY_GATES must be valid JSON") from None
    if not isinstance(decoded, list):
        raise InvalidGateSpecError("FACTORY_QUALITY_GATES must be a JSON array")
    return _parse_gate_items(decoded)


def _parse_projects(raw: str) -> ProjectRegistry:
    """Parse operator owned project profiles; provider content never reaches this path."""
    try:
        decoded = json.loads(raw)
        if not isinstance(decoded, list):
            raise ValueError
        profiles: list[ProjectProfile] = []
        for item in decoded:
            if not isinstance(item, dict) or not isinstance(item.get("gates"), list):
                raise ValueError
            profiles.append(
                ProjectProfile(
                    project_id=item["project_id"],
                    repository_slug=item["repository"],
                    source_checkout=item["source_checkout"],
                    base_ref=item["base_ref"],
                    gates=_parse_gate_items(item["gates"]),
                    context_profile=item.get("context_profile", "repository"),
                    deploy_policy=item.get("deploy_policy", "human-only"),
                    git_host=item.get("git_host", "github.com"),
                    deploy_required=item.get("deploy_required", False),
                    provider_ci_required=item.get("provider_ci_required", True),
                    required_ci_checks=tuple(item.get("required_ci_checks", ())),
                )
            )
        return ProjectRegistry(tuple(profiles))
    except (KeyError, TypeError, ValueError):
        raise ValueError("FACTORY_PROJECTS contains an invalid project profile") from None


def _parse_gate_items(decoded: list[object]) -> tuple[QualityGateSpec, ...]:
    specs: list[QualityGateSpec] = []
    seen: set[str] = set()
    for index, item in enumerate(decoded):
        if not isinstance(item, dict):
            raise InvalidGateSpecError(f"quality gate #{index} must be a JSON object")
        name = item.get("name")
        argv = item.get("argv")
        required = item.get("required", True)
        if not isinstance(name, str) or not name.strip():
            raise InvalidGateSpecError(f"quality gate #{index} needs a non-empty name")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
            raise InvalidGateSpecError(f"quality gate {name!r} needs a non-empty argv list")
        if not isinstance(required, bool):
            raise InvalidGateSpecError(f"quality gate {name!r} 'required' must be a boolean")
        if name in seen:
            raise InvalidGateSpecError(f"quality gate {name!r} is duplicated")
        seen.add(name)
        specs.append(QualityGateSpec(name=name, argv=tuple(argv), required=required))
    return tuple(specs)


def parse_task_gate_specs(raw: str | None) -> dict[str, tuple[QualityGateSpec, ...]]:
    """Parse operator-owned, issue-specific gates keyed by source reference."""
    cleaned = _clean(raw)
    if cleaned is None:
        return {}
    try:
        decoded = json.loads(cleaned)
    except json.JSONDecodeError:
        raise InvalidGateSpecError("FACTORY_TASK_QUALITY_GATES must be valid JSON") from None
    if not isinstance(decoded, dict):
        raise InvalidGateSpecError("FACTORY_TASK_QUALITY_GATES must be a JSON object")
    parsed: dict[str, tuple[QualityGateSpec, ...]] = {}
    for ref, items in decoded.items():
        if not isinstance(ref, str) or not isinstance(items, list):
            raise InvalidGateSpecError("FACTORY_TASK_QUALITY_GATES entries must be gate arrays")
        parsed[ref] = _parse_gate_items(items)
    return parsed


__all__ = [
    "DEFAULT_DATABASE_PATH",
    "DEFAULT_GITHUB_API_URL",
    "DEFAULT_GITHUB_GIT_HOST",
    "DEFAULT_TARGET_BRANCH",
    "DEFAULT_RUN_POLL_INTERVAL",
    "DEFAULT_RUN_TIMEOUT",
    "DEFAULT_WORKSPACE_ROOT",
    "DEFAULT_OPENHANDS_WORKSPACE_ROOT",
    "AgentConfig",
    "AgentEngine",
    "DatabaseConfig",
    "DatabaseScheme",
    "Environment",
    "FactoryConfig",
    "GitHubConfig",
    "InvalidGateSpecError",
    "LogFormat",
    "LoggingConfig",
    "UnsupportedDatabaseError",
    "parse_gate_specs",
    "parse_task_gate_specs",
]
