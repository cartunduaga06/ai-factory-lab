"""Tests for backend selection and Cloud configuration redaction.

No test reads the real environment or the network: every configuration is passed
as an explicit mapping, and the backend builder is exercised with configuration
only (its adapters are never called).
"""

from __future__ import annotations

import pytest

from factory.backend import BackendConfigurationError, build_backend, cloud_remote
from factory.domain.enums import AgentBackend
from factory.infrastructure.config import (
    DEFAULT_OPENHANDS_CLOUD_API_URL,
    DEFAULT_OPENHANDS_CLOUD_WORKING_DIR,
    FactoryConfig,
    InvalidBackendError,
    OpenHandsCloudConfig,
    parse_backend,
)
from factory.integrations.openhands import OpenHandsAdapter, OpenHandsCloudAdapter
from factory.integrations.workspace import (
    CloudWorkspaceProvisioner,
    GitWorktreeWorkspaceProvisioner,
)

CLOUD_KEY = "cloud-key-must-not-leak"
PROFILE_ID = "d6934b00-ab23-4aed-bf84-14463e77f6e8"
REPOSITORY = "cartunduaga06/ai-factory-lab"


# -- backend parsing -------------------------------------------------------


def test_backend_defaults_to_local_and_never_cloud() -> None:
    assert parse_backend(None) is AgentBackend.LOCAL
    assert FactoryConfig.from_env({}).backend is AgentBackend.LOCAL


def test_backend_accepts_an_explicit_choice() -> None:
    assert parse_backend("cloud") is AgentBackend.CLOUD
    assert parse_backend("LOCAL") is AgentBackend.LOCAL


def test_unknown_backend_fails_closed() -> None:
    with pytest.raises(InvalidBackendError):
        parse_backend("gpu")
    with pytest.raises(InvalidBackendError):
        FactoryConfig.from_env({"OPENHANDS_BACKEND": "gpu"})


# -- cloud configuration ---------------------------------------------------


def test_cloud_config_is_disabled_without_a_credential_or_repository() -> None:
    assert FactoryConfig.from_env({}).openhands_cloud.enabled is False
    only_key = OpenHandsCloudConfig(api_key=CLOUD_KEY)
    assert only_key.enabled is False
    only_repo = OpenHandsCloudConfig(repository=REPOSITORY)
    assert only_repo.enabled is False
    both = OpenHandsCloudConfig(api_key=CLOUD_KEY, repository=REPOSITORY)
    assert both.enabled is False
    complete = OpenHandsCloudConfig(api_key=CLOUD_KEY, repository=REPOSITORY, profile=PROFILE_ID)
    assert complete.enabled is True


def test_cloud_defaults_are_safe_and_do_not_break_local_mode() -> None:
    config = FactoryConfig.from_env({})
    assert config.openhands_cloud.api_url == DEFAULT_OPENHANDS_CLOUD_API_URL
    assert config.openhands_cloud.working_dir == DEFAULT_OPENHANDS_CLOUD_WORKING_DIR
    assert config.openhands_cloud.api_key is None
    assert config.openhands_cloud.repository is None
    assert config.backend is AgentBackend.LOCAL


def test_cloud_configuration_loads_from_the_environment() -> None:
    config = FactoryConfig.from_env(
        {
            "OPENHANDS_BACKEND": "cloud",
            "OPENHANDS_CLOUD_API_KEY": CLOUD_KEY,
            "OPENHANDS_CLOUD_REPOSITORY": REPOSITORY,
            "OPENHANDS_CLOUD_WORKING_DIR": "/srv/project",
            "OPENHANDS_CLOUD_PROFILE": PROFILE_ID,
            "OPENHANDS_CLOUD_MODEL": "some-model",
        }
    )
    assert config.backend is AgentBackend.CLOUD
    assert config.openhands_cloud.api_key == CLOUD_KEY
    assert config.openhands_cloud.repository == REPOSITORY
    assert config.openhands_cloud.working_dir == "/srv/project"
    assert config.openhands_cloud.profile == PROFILE_ID
    assert config.openhands_cloud.model == "some-model"


def test_cloud_placeholder_is_treated_as_unset() -> None:
    config = FactoryConfig.from_env({"OPENHANDS_CLOUD_API_KEY": "<your-cloud-key>"})
    assert config.openhands_cloud.api_key is None
    assert config.openhands_cloud.enabled is False


def test_redacted_masks_the_cloud_credential_and_backend() -> None:
    config = FactoryConfig.from_env(
        {
            "OPENHANDS_BACKEND": "cloud",
            "OPENHANDS_CLOUD_API_KEY": CLOUD_KEY,
            "OPENHANDS_CLOUD_REPOSITORY": REPOSITORY,
            "OPENHANDS_CLOUD_PROFILE": PROFILE_ID,
            "FACTORY_TARGET_REPO": REPOSITORY,
        }
    )
    redacted = config.redacted()
    serialized = repr(redacted)
    assert CLOUD_KEY not in serialized
    cloud = redacted["openhands_cloud"]  # type: ignore[index]
    assert cloud["api_key"] == "***"
    assert cloud["repository"] == REPOSITORY
    assert redacted["backend"] == "cloud"


def test_redacted_cloud_api_key_is_none_when_unset() -> None:
    cloud = FactoryConfig.from_env({}).redacted()["openhands_cloud"]  # type: ignore[index]
    assert cloud["api_key"] is None


# -- backend building ------------------------------------------------------


def _config(**overrides: object) -> FactoryConfig:
    base: dict[str, object] = {
        "FACTORY_SOURCE_CHECKOUT": "/srv/checkout",
        "OPENHANDS_WORKSPACE_ROOT": "/srv/workspaces",
        "FACTORY_TARGET_REPO": REPOSITORY,
    }
    base.update(overrides)
    return FactoryConfig.from_env(base)


def test_build_local_backend_uses_the_self_hosted_adapter_and_worktree() -> None:
    config = _config(
        OPENHANDS_BASE_URL="http://localhost:60000",
        OPENHANDS_AGENT_PROFILE_ID="profile-1",
    )
    backend = build_backend(config)
    assert isinstance(backend.adapter, OpenHandsAdapter)
    assert isinstance(backend.provisioner, GitWorktreeWorkspaceProvisioner)


def test_build_cloud_backend_uses_the_cloud_adapter_and_repo_provisioner() -> None:
    config = _config(
        OPENHANDS_BACKEND="cloud",
        OPENHANDS_CLOUD_API_KEY=CLOUD_KEY,
        OPENHANDS_CLOUD_REPOSITORY=REPOSITORY,
        OPENHANDS_CLOUD_PROFILE=PROFILE_ID,
    )
    backend = build_backend(config)
    assert isinstance(backend.adapter, OpenHandsCloudAdapter)
    assert isinstance(backend.provisioner, CloudWorkspaceProvisioner)


def test_cloud_backend_requires_full_cloud_configuration() -> None:
    config = _config(OPENHANDS_BACKEND="cloud")
    with pytest.raises(BackendConfigurationError) as caught:
        build_backend(config)
    message = str(caught.value)
    assert "OPENHANDS_CLOUD_API_KEY" in message
    assert CLOUD_KEY not in message


def test_missing_cloud_credentials_never_break_local_mode() -> None:
    """Local mode with no Cloud configuration at all still builds."""
    config = _config(
        OPENHANDS_BASE_URL="http://localhost:60000",
        OPENHANDS_AGENT_PROFILE_ID="profile-1",
    )
    backend = build_backend(config)
    assert isinstance(backend.adapter, OpenHandsAdapter)


def test_local_backend_requires_a_base_url_and_profile() -> None:
    with pytest.raises(BackendConfigurationError):
        build_backend(_config())
    with pytest.raises(BackendConfigurationError):
        build_backend(_config(OPENHANDS_BASE_URL="http://localhost:60000"))


def test_build_backend_requires_a_source_checkout() -> None:
    config = FactoryConfig.from_env(
        {
            "OPENHANDS_BACKEND": "cloud",
            "OPENHANDS_CLOUD_API_KEY": CLOUD_KEY,
            "OPENHANDS_CLOUD_REPOSITORY": REPOSITORY,
        }
    )
    with pytest.raises(BackendConfigurationError):
        build_backend(config)


def test_cloud_remote_is_built_from_the_repository_slug() -> None:
    assert cloud_remote(OpenHandsCloudConfig(repository=REPOSITORY)) == (
        f"https://github.com/{REPOSITORY}"
    )
    assert cloud_remote(OpenHandsCloudConfig(repository=f"{REPOSITORY}.git")) == (
        f"https://github.com/{REPOSITORY}"
    )


@pytest.mark.parametrize("repository", ["no-slash", "a/b/c", "../etc/passwd", "a/"])
def test_cloud_remote_refuses_a_malformed_repository(repository: str) -> None:
    with pytest.raises(BackendConfigurationError):
        cloud_remote(OpenHandsCloudConfig(repository=repository))


def test_cloud_backend_refuses_repository_mismatch() -> None:
    config = _config(
        OPENHANDS_BACKEND="cloud",
        OPENHANDS_CLOUD_API_KEY=CLOUD_KEY,
        OPENHANDS_CLOUD_REPOSITORY="someone/else",
        OPENHANDS_CLOUD_PROFILE=PROFILE_ID,
    )
    with pytest.raises(BackendConfigurationError, match="exactly match"):
        build_backend(config)


def test_cloud_backend_requires_agent_profile_uuid() -> None:
    config = _config(
        OPENHANDS_BACKEND="cloud",
        OPENHANDS_CLOUD_API_KEY=CLOUD_KEY,
        OPENHANDS_CLOUD_REPOSITORY=REPOSITORY,
        OPENHANDS_CLOUD_PROFILE="deepseek-v4.1-flash",
    )
    with pytest.raises(BackendConfigurationError, match="Agent Profile UUID"):
        build_backend(config)


def test_cloud_backend_rejects_unsupported_direct_model_selector() -> None:
    config = _config(
        OPENHANDS_BACKEND="cloud",
        OPENHANDS_CLOUD_API_KEY=CLOUD_KEY,
        OPENHANDS_CLOUD_REPOSITORY=REPOSITORY,
        OPENHANDS_CLOUD_PROFILE=PROFILE_ID,
        OPENHANDS_CLOUD_MODEL="openhands/deepseek-v4.1-flash",
    )
    with pytest.raises(BackendConfigurationError, match="not a supported Agent Server selector"):
        build_backend(config)
