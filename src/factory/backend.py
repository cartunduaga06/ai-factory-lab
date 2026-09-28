"""Application-layer backend selection.

The factory has two execution backends behind the single
:class:`~factory.domain.models.AgentAdapter` contract:

* ``local`` — the existing self-hosted Agent Server plus a local Git worktree.
* ``cloud`` — OpenHands Cloud, whose run is materialised back into a local Git
  worktree at the exact revision before validation.

This module is the *only* place that knows both concrete backends. It sits in the
application layer, next to the CLI, so neither the domain nor the orchestration
layer ever learns which backend is configured. Selection is explicit and comes
from ``OPENHANDS_BACKEND``; there is no implicit or automatic fallback between
backends.
"""

from __future__ import annotations

from dataclasses import dataclass

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
    """The selected backend is not usable with the current configuration.

    Raised at startup, before any task is claimed, so a misconfigured backend
    fails closed with a clear operator message instead of failing mid-run.
    """


@dataclass(slots=True)
class Backend:
    """The concrete objects one backend selection resolves to."""

    adapter: AgentAdapter
    provisioner: WorkspaceProvisioner


def build_backend(config: FactoryConfig) -> Backend:
    """Resolve the configured backend into an adapter and a provisioner.

    The default is :attr:`~factory.domain.enums.AgentBackend.LOCAL`. Cloud is only
    ever built when explicitly selected *and* fully configured; otherwise this
    raises :class:`BackendConfigurationError` and nothing runs. There is no
    fallback in either direction.
    """
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
            "the cloud backend requires OPENHANDS_CLOUD_API_KEY and OPENHANDS_CLOUD_REPOSITORY"
        )
    assert config.source_checkout is not None
    assert cloud.api_key is not None
    assert cloud.repository is not None
    try:
        control = CloudControlClient(cloud.api_url, api_key=cloud.api_key)
        adapter = OpenHandsCloudAdapter(
            control,
            CloudExecution(
                working_dir=cloud.working_dir,
                repository=cloud.repository,
                profile=cloud.profile,
                model=cloud.model,
            ),
            GitCloudRevisionProvider(config.source_checkout),
        )
    except OpenHandsCloudConfigurationError as exc:
        raise BackendConfigurationError(str(exc)) from None
    provisioner = CloudWorkspaceProvisioner(
        config.source_checkout,
        remote=cloud_remote(cloud),
        base_ref=config.workspace_base_ref,
    )
    return Backend(adapter=adapter, provisioner=provisioner)


def cloud_remote(cloud: OpenHandsCloudConfig) -> str:
    """Render a repository slug as the HTTPS remote a Cloud run is cloned from."""
    assert cloud.repository is not None
    slug = cloud.repository.strip().removesuffix(".git")
    if slug.count("/") != 1 or any(part in {"", ".", ".."} for part in slug.split("/")):
        raise BackendConfigurationError(
            "OPENHANDS_CLOUD_REPOSITORY must be an owner/name repository slug"
        )
    return f"https://github.com/{slug}"


__all__ = [
    "Backend",
    "BackendConfigurationError",
    "build_backend",
    "cloud_remote",
]
