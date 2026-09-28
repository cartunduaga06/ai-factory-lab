"""Application-layer backend selection."""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from factory.domain.enums import AgentBackend
from factory.domain.models import AgentAdapter
from factory.domain.ports import WorkspaceProvisioner
from factory.infrastructure.config import FactoryConfig, OpenHandsCloudConfig
from factory.integrations.openhands import (
    CloudControlClient,
    CloudExecution,
    OpenHandsAdapter,
    OpenHandsClient,
    OpenHandsCloudAdapter,
    OpenHandsExecution,
    WorkspacePathMapper,
)
from factory.integrations.openhands.cloud import OpenHandsCloudConfigurationError
from factory.integrations.workspace import (
    CloudWorkspaceProvisioner,
    GitCloudRevisionProvider,
    GitWorktreeWorkspaceProvisioner,
)


class BackendConfigurationError(Exception):
    """The selected backend is not usable with the current configuration."""


@dataclass(slots=True)
class Backend:
    """The concrete objects one backend selection resolves to."""

    adapter: AgentAdapter
    provisioner: WorkspaceProvisioner


def build_backend(config: FactoryConfig) -> Backend:
    """Resolve the configured backend into an adapter and a provisioner."""
    if config.source_checkout is None:
        raise BackendConfigurationError("FACTORY_SOURCE_CHECKOUT is required for run")
    if config.backend is AgentBackend.CLOUD:
        return _build_cloud(config)
    return _build_local(config)


def _build_local(config: FactoryConfig) -> Backend:
    if config.openhands.base_url is None:
        raise BackendConfigurationError("OPENHANDS_BASE_URL is required for the local backend")
    if config.openhands.agent_profile_id is None:
        raise BackendConfigurationError(
            "OPENHANDS_AGENT_PROFILE_ID is required for the local backend"
        )
    assert config.source_checkout is not None
    mapper = WorkspacePathMapper(config.workspace_root, config.openhands_workspace_root)
    adapter = OpenHandsAdapter(
        OpenHandsClient(
            config.openhands.base_url,
            session_api_key=config.openhands.session_api_key,
        ),
        OpenHandsExecution(
            agent_profile_id=config.openhands.agent_profile_id,
            shared_workspace_hook_command=config.openhands_shared_workspace_hook_command,
        ),
        workspace_paths=mapper,
    )
    provisioner = GitWorktreeWorkspaceProvisioner(
        config.source_checkout, base_ref=config.workspace_base_ref
    )
    return Backend(adapter=adapter, provisioner=provisioner)


def _build_cloud(config: FactoryConfig) -> Backend:
    cloud = config.openhands_cloud
    if not cloud.enabled:
        raise BackendConfigurationError(
            "the cloud backend requires OPENHANDS_CLOUD_API_KEY, "
            "OPENHANDS_CLOUD_REPOSITORY and OPENHANDS_CLOUD_PROFILE"
        )
    if config.github.target_repo is None:
        raise BackendConfigurationError("FACTORY_TARGET_REPO is required for the cloud backend")

    assert config.source_checkout is not None
    assert cloud.api_key is not None
    assert cloud.repository is not None
    assert cloud.profile is not None

    cloud_slug = _cloud_slug(cloud)
    if cloud_slug != config.github.target_repo:
        raise BackendConfigurationError(
            "OPENHANDS_CLOUD_REPOSITORY must exactly match FACTORY_TARGET_REPO"
        )
    try:
        UUID(cloud.profile)
    except ValueError:
        raise BackendConfigurationError(
            "OPENHANDS_CLOUD_PROFILE must be an exact Agent Profile UUID"
        ) from None
    if cloud.model is not None:
        raise BackendConfigurationError(
            "OPENHANDS_CLOUD_MODEL is not a supported Agent Server selector; "
            "select the desired model in the explicit OPENHANDS_CLOUD_PROFILE instead"
        )

    base_ref = cloud.base_ref or config.workspace_base_ref or config.target_default_branch
    try:
        control = CloudControlClient(cloud.api_url, api_key=cloud.api_key)
        adapter = OpenHandsCloudAdapter(
            control,
            CloudExecution(
                working_dir=cloud.working_dir,
                repository=cloud_slug,
                profile=cloud.profile,
                base_ref=base_ref,
            ),
            GitCloudRevisionProvider(
                config.source_checkout,
                read_token=config.github.token,
            ),
        )
    except OpenHandsCloudConfigurationError as exc:
        raise BackendConfigurationError(str(exc)) from None

    provisioner = CloudWorkspaceProvisioner(
        config.source_checkout,
        base_ref=base_ref,
    )
    return Backend(adapter=adapter, provisioner=provisioner)


def _cloud_slug(cloud: OpenHandsCloudConfig) -> str:
    assert cloud.repository is not None
    slug = cloud.repository.strip().removesuffix(".git")
    if slug.count("/") != 1 or any(part in {"", ".", ".."} for part in slug.split("/")):
        raise BackendConfigurationError(
            "OPENHANDS_CLOUD_REPOSITORY must be an owner/name repository slug"
        )
    return slug


def cloud_remote(cloud: OpenHandsCloudConfig) -> str:
    """Render the validated Cloud repository slug as a credential-free HTTPS URL."""
    return f"https://github.com/{_cloud_slug(cloud)}"


__all__ = [
    "Backend",
    "BackendConfigurationError",
    "build_backend",
    "cloud_remote",
]
