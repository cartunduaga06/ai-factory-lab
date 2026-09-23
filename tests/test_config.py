"""Tests for environment-driven configuration.

Every test passes an explicit mapping so no real environment or secret is read.
"""

from __future__ import annotations

from factory.infrastructure.config import (
    DEFAULT_GITHUB_API_URL,
    DEFAULT_WORKSPACE_ROOT,
    Environment,
    FactoryConfig,
    LogFormat,
)


def test_defaults_are_safe_with_empty_environment() -> None:
    config = FactoryConfig.from_env({})
    assert config.environment is Environment.DEVELOPMENT
    assert config.github.token is None
    assert config.github.enabled is False
    assert config.github.api_url == DEFAULT_GITHUB_API_URL
    assert config.openhands.enabled is False
    assert config.codex.enabled is False
    assert config.database_url is None
    assert config.workspace_root == DEFAULT_WORKSPACE_ROOT
    assert config.logging.level == "INFO"
    assert config.logging.fmt is LogFormat.TEXT


def test_placeholders_are_treated_as_unset() -> None:
    config = FactoryConfig.from_env(
        {
            "GITHUB_TOKEN": "<your-github-token>",
            "OPENHANDS_API_KEY": "<your-openhands-api-key>",
            "DATABASE_URL": "",
        }
    )
    assert config.github.token is None
    assert config.openhands.api_key is None
    assert config.database_url is None


def test_real_values_are_loaded() -> None:
    config = FactoryConfig.from_env(
        {
            "GITHUB_TOKEN": "ghp_example_value",
            "FACTORY_GITHUB_REPO": "cartunduaga06/ai-factory-lab",
            "FACTORY_TARGET_REPO": "cartunduaga06/finanza-ia",
            "OPENHANDS_API_KEY": "oh_example_value",
            "OPENHANDS_BASE_URL": "https://example.invalid",
            "DATABASE_URL": "sqlite+pysqlite:///./factory.db",
            "FACTORY_LOG_LEVEL": "debug",
            "FACTORY_LOG_FORMAT": "text",
        }
    )
    assert config.github.enabled is True
    assert config.github.control_plane_repo == "cartunduaga06/ai-factory-lab"
    assert config.github.target_repo == "cartunduaga06/finanza-ia"
    assert config.openhands.enabled is True
    assert config.database_url == "sqlite+pysqlite:///./factory.db"
    assert config.logging.level == "DEBUG"


def test_redacted_never_leaks_credentials() -> None:
    config = FactoryConfig.from_env(
        {
            "GITHUB_TOKEN": "ghp_example_value",
            "OPENHANDS_API_KEY": "oh_example_value",
            "CODEX_API_KEY": "codex_example_value",
        }
    )
    redacted = config.redacted()
    serialized = repr(redacted)
    assert "ghp_example_value" not in serialized
    assert "oh_example_value" not in serialized
    assert "codex_example_value" not in serialized
    assert redacted["github"]["token"] == "***"  # type: ignore[index]


def test_unknown_enum_values_fall_back_to_defaults() -> None:
    config = FactoryConfig.from_env({"FACTORY_ENV": "banana", "FACTORY_LOG_FORMAT": "xml"})
    assert config.environment is Environment.DEVELOPMENT
    assert config.logging.fmt is LogFormat.TEXT
