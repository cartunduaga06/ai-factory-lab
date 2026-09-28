"""Concrete workspace provisioning integrations.

A workspace is a physical, isolated checkout. Materialising one requires the
filesystem and a version-control tool, so it lives here — outside ``domain`` and
``orchestration`` — behind the
:class:`~factory.domain.ports.WorkspaceProvisioner` port.
"""

from factory.integrations.workspace.git import GitWorktreeWorkspaceProvisioner
from factory.integrations.workspace.git_publish import GitWorkspacePublisher
from factory.integrations.workspace.revision import GitWorkspaceRevisionInspector
from factory.integrations.workspace.shared_policy import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_EXECUTABLE_MODE,
    PRIVATE_FILE_MODE,
    SHARED_DIRECTORY_MODE,
    SHARED_EXECUTABLE_MODE,
    SHARED_FILE_MODE,
    SharedWorkspacePolicyError,
    classify_ignored_paths,
    normalize_workspace,
    normalize_workspace_path,
    target_directory_mode,
    target_file_mode,
)

__all__ = [
    "PRIVATE_DIRECTORY_MODE",
    "PRIVATE_EXECUTABLE_MODE",
    "PRIVATE_FILE_MODE",
    "SHARED_DIRECTORY_MODE",
    "SHARED_EXECUTABLE_MODE",
    "SHARED_FILE_MODE",
    "GitWorkspacePublisher",
    "GitWorkspaceRevisionInspector",
    "GitWorktreeWorkspaceProvisioner",
    "SharedWorkspacePolicyError",
    "classify_ignored_paths",
    "normalize_workspace",
    "normalize_workspace_path",
    "target_directory_mode",
    "target_file_mode",
]
