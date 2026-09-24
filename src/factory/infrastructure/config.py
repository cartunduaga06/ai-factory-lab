"""Configuration model for AI Factory Lab.

Configuration is read from environment variables only. Secrets are never
hard-coded, logged or defaulted to real values: a missing credential surfaces
as ``None`` until a component that needs it actually runs. This lets the
repository be cloned and tested with zero integrations configured.

See ``.env.example`` for the full, placeholder-only reference.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

DEFAULT_GITHUB_API_URL = "https://api.github.com"
DEFAULT_WORKSPACE_ROOT = "./.workspaces"
DEFAULT_DATABASE_PATH = "./factory.db"


class DatabaseScheme(StrEnum):
    """Supported persistence backends."""

    SQLITE = "sqlite"


class UnsupportedDatabaseError(ValueError):
    """Raised when ``DATABASE_URL`` names a scheme the factory cannot use yet."""


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


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
    database_url: str | None
    workspace_root: str
    logging: LoggingConfig

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
        raw_env = (_clean(source.get("FACTORY_ENV")) or Environment.DEVELOPMENT.value).lower()
        try:
            environment = Environment(raw_env)
        except ValueError:
            environment = Environment.DEVELOPMENT
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
            database_url=_clean(source.get("DATABASE_URL")),
            workspace_root=_clean(source.get("FACTORY_WORKSPACE_ROOT")) or DEFAULT_WORKSPACE_ROOT,
            logging=LoggingConfig.from_env(source),
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
            "database_url": _redact_url(self.database_url),
            "workspace_root": self.workspace_root,
            "logging": {"level": self.logging.level, "format": self.logging.fmt.value},
        }


__all__ = [
    "DEFAULT_DATABASE_PATH",
    "DEFAULT_GITHUB_API_URL",
    "DEFAULT_WORKSPACE_ROOT",
    "AgentConfig",
    "DatabaseConfig",
    "DatabaseScheme",
    "Environment",
    "FactoryConfig",
    "GitHubConfig",
    "LogFormat",
    "LoggingConfig",
    "UnsupportedDatabaseError",
]
