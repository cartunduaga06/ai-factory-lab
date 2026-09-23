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


class Environment(StrEnum):
    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class LogFormat(StrEnum):
    TEXT = "text"
    JSON = "json"


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
    """Connection details for a single agent execution engine."""

    api_key: str | None
    base_url: str | None = None

    @property
    def enabled(self) -> bool:
        return self.api_key is not None


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
class FactoryConfig:
    """Top-level, validated configuration for the whole factory."""

    environment: Environment
    github: GitHubConfig
    openhands: AgentConfig
    codex: AgentConfig
    database_url: str | None
    workspace_root: str
    logging: LoggingConfig

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
                "api_key": "***" if self.openhands.api_key else None,
            },
            "codex": {"api_key": "***" if self.codex.api_key else None},
            "database_url": self.database_url,
            "workspace_root": self.workspace_root,
            "logging": {"level": self.logging.level, "format": self.logging.fmt.value},
        }


__all__ = [
    "DEFAULT_GITHUB_API_URL",
    "DEFAULT_WORKSPACE_ROOT",
    "AgentConfig",
    "Environment",
    "FactoryConfig",
    "GitHubConfig",
    "LogFormat",
    "LoggingConfig",
]
