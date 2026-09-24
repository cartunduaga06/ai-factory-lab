"""Tests for environment-driven configuration.

Every test passes an explicit mapping so no real environment or secret is read.
"""

from __future__ import annotations

import pytest

from factory.infrastructure.config import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_GITHUB_API_URL,
    DEFAULT_WORKSPACE_ROOT,
    DatabaseScheme,
    Environment,
    FactoryConfig,
    LogFormat,
    UnsupportedDatabaseError,
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
            "OPENHANDS_SESSION_API_KEY": "session_example_value",
            "CODEX_API_KEY": "codex_example_value",
        }
    )
    redacted = config.redacted()
    serialized = repr(redacted)
    assert "ghp_example_value" not in serialized
    assert "oh_example_value" not in serialized
    assert "session_example_value" not in serialized
    assert "codex_example_value" not in serialized
    assert redacted["github"]["token"] == "***"  # type: ignore[index]


# -- openhands session configuration ---------------------------------------


def test_openhands_session_key_is_optional() -> None:
    config = FactoryConfig.from_env({"OPENHANDS_BASE_URL": "http://localhost:60000"})
    assert config.openhands.session_api_key is None
    assert config.openhands.base_url == "http://localhost:60000"


def test_openhands_session_key_loads() -> None:
    config = FactoryConfig.from_env({"OPENHANDS_SESSION_API_KEY": "session_example_value"})
    assert config.openhands.session_api_key == "session_example_value"


def test_openhands_session_placeholder_is_treated_as_unset() -> None:
    config = FactoryConfig.from_env({"OPENHANDS_SESSION_API_KEY": "<your-session-key>"})
    assert config.openhands.session_api_key is None


def test_redacted_masks_the_openhands_session_key() -> None:
    config = FactoryConfig.from_env({"OPENHANDS_SESSION_API_KEY": "session_example_value"})
    redacted = config.redacted()["openhands"]  # type: ignore[index]
    assert redacted["session_api_key"] == "***"
    assert "session_example_value" not in repr(config.redacted())


def test_unknown_enum_values_fall_back_to_defaults() -> None:
    config = FactoryConfig.from_env({"FACTORY_ENV": "banana", "FACTORY_LOG_FORMAT": "xml"})
    assert config.environment is Environment.DEVELOPMENT
    assert config.logging.fmt is LogFormat.TEXT


# -- database URL parsing --------------------------------------------------


def test_sqlite_url_is_parsed() -> None:
    config = FactoryConfig.from_env({"DATABASE_URL": "sqlite:///./factory.db"})
    parsed = config.database
    assert parsed.scheme is DatabaseScheme.SQLITE
    assert parsed.path == "./factory.db"
    assert parsed.url == "sqlite:///./factory.db"


def test_sqlalchemy_style_sqlite_url_is_accepted() -> None:
    config = FactoryConfig.from_env({"DATABASE_URL": "sqlite+pysqlite:///./factory.db"})
    assert config.database.path == "./factory.db"


def test_absolute_sqlite_path_is_preserved() -> None:
    config = FactoryConfig.from_env({"DATABASE_URL": "sqlite:////var/lib/factory.db"})
    assert config.database.path == "/var/lib/factory.db"


def test_missing_database_url_defaults_to_local_sqlite() -> None:
    config = FactoryConfig.from_env({})
    assert config.database.scheme is DatabaseScheme.SQLITE
    assert config.database.path == DEFAULT_DATABASE_PATH


def test_unsupported_database_scheme_is_rejected() -> None:
    config = FactoryConfig.from_env({"DATABASE_URL": "postgresql://localhost/factory"})
    with pytest.raises(UnsupportedDatabaseError):
        _ = config.database


def test_malformed_database_url_is_rejected() -> None:
    config = FactoryConfig.from_env({"DATABASE_URL": "not-a-url"})
    with pytest.raises(UnsupportedDatabaseError):
        _ = config.database


def test_sqlite_url_without_path_is_rejected() -> None:
    config = FactoryConfig.from_env({"DATABASE_URL": "sqlite:///"})
    with pytest.raises(UnsupportedDatabaseError):
        _ = config.database


def test_loading_config_does_not_require_a_valid_database_url() -> None:
    # Parsing is deferred: reading config alone must never raise.
    config = FactoryConfig.from_env({"DATABASE_URL": "postgresql://localhost/factory"})
    assert config.database_url == "postgresql://localhost/factory"


# -- github runtime configuration ------------------------------------------


def test_missing_github_token_means_github_is_disabled() -> None:
    config = FactoryConfig.from_env({})
    assert config.github.token is None
    assert config.github.enabled is False


def test_github_repository_settings_load() -> None:
    config = FactoryConfig.from_env(
        {
            "GITHUB_TOKEN": "ghp_example",
            "FACTORY_GITHUB_REPO": "cartunduaga06/ai-factory-lab",
            "FACTORY_TARGET_REPO": "cartunduaga06/finanza-ia",
        }
    )
    assert config.github.enabled is True
    assert config.github.control_plane_repo == "cartunduaga06/ai-factory-lab"
    assert config.github.target_repo == "cartunduaga06/finanza-ia"


def test_redacted_database_url_does_not_leak_a_token() -> None:
    config = FactoryConfig.from_env({"DATABASE_URL": "sqlite:///./factory.db"})
    assert config.redacted()["database_url"] == "sqlite:///./factory.db"


def test_redacted_database_url_masks_embedded_userinfo() -> None:
    config = FactoryConfig.from_env(
        {"DATABASE_URL": "postgresql://user:supersecret@db.internal:5432/factory"}
    )
    redacted = config.redacted()["database_url"]
    assert "supersecret" not in str(redacted)
    assert "user" not in str(redacted)
    assert "db.internal:5432/factory" in str(redacted)


# -- source checkout and quality gates (Phase 4) ---------------------------


def test_source_checkout_is_unset_by_default_and_injected_when_configured() -> None:
    assert FactoryConfig.from_env({}).source_checkout is None

    config = FactoryConfig.from_env({"FACTORY_SOURCE_CHECKOUT": "/srv/checkout/target"})
    assert config.source_checkout == "/srv/checkout/target"
    assert config.redacted()["source_checkout"] == "/srv/checkout/target"


def test_quality_gates_default_to_none_configured() -> None:
    # Documented behaviour: no gates means nothing the factory invented.
    assert FactoryConfig.from_env({}).quality_gates == ()


def test_quality_gates_are_parsed_from_argv_json() -> None:
    config = FactoryConfig.from_env(
        {
            "FACTORY_QUALITY_GATES": (
                '[{"name": "tests", "argv": ["pytest"]},'
                ' {"name": "lint", "argv": ["ruff", "check", "."], "required": false}]'
            )
        }
    )
    assert [spec.name for spec in config.quality_gates] == ["tests", "lint"]
    assert config.quality_gates[0].argv == ("pytest",)
    assert config.quality_gates[0].required is True
    assert config.quality_gates[1].required is False


def test_quality_gate_redacted_view_omits_argv() -> None:
    config = FactoryConfig.from_env(
        {"FACTORY_QUALITY_GATES": '[{"name": "tests", "argv": ["pytest", "-x"]}]'}
    )
    rendered = config.redacted()["quality_gates"]
    assert rendered == [{"name": "tests", "required": True}]
    assert "-x" not in repr(rendered)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        '{"name": "tests", "argv": ["pytest"]}',
        '[{"name": "", "argv": ["pytest"]}]',
        '[{"name": "tests", "argv": []}]',
        '[{"name": "tests", "argv": "pytest"}]',
        '[{"name": "tests", "argv": ["pytest"], "required": "yes"}]',
        "[42]",
    ],
)
def test_malformed_quality_gate_config_is_rejected(raw: str) -> None:
    from factory.infrastructure.config import InvalidGateSpecError

    with pytest.raises(InvalidGateSpecError):
        FactoryConfig.from_env({"FACTORY_QUALITY_GATES": raw})
