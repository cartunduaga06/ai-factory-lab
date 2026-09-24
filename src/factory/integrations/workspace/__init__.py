"""Concrete workspace provisioning integrations.

A workspace is a physical, isolated checkout. Materialising one requires the
filesystem and a version-control tool, so it lives here — outside ``domain`` and
``orchestration`` — behind the
:class:`~factory.domain.ports.WorkspaceProvisioner` port.
"""

from factory.integrations.workspace.git import GitWorktreeWorkspaceProvisioner
from factory.integrations.workspace.git_publish import GitWorkspacePublisher

__all__ = ["GitWorkspacePublisher", "GitWorktreeWorkspaceProvisioner"]
