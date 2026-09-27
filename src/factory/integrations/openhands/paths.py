"""Deterministic translation between host and OpenHands container paths."""

from __future__ import annotations

from pathlib import Path


class WorkspacePathError(ValueError):
    """A workspace is not inside the configured host workspace root."""


class WorkspacePathMapper:
    """Map factory-owned host workspace paths into the agent container."""

    def __init__(self, host_root: str, container_root: str) -> None:
        host = Path(host_root).expanduser().resolve()
        container = Path(container_root)
        if not host_root.strip() or not container_root.strip():
            raise WorkspacePathError("workspace roots must not be empty")
        if not container.is_absolute():
            raise WorkspacePathError("OpenHands workspace root must be absolute")
        self._host_root = host
        self._container_root = container

    @property
    def host_root(self) -> str:
        return str(self._host_root)

    @property
    def container_root(self) -> str:
        return str(self._container_root)

    def to_container(self, host_path: str) -> str:
        """Translate one host path, refusing traversal outside the root."""
        candidate = Path(host_path).expanduser().resolve()
        try:
            relative = candidate.relative_to(self._host_root)
        except ValueError:
            raise WorkspacePathError("workspace path is outside FACTORY_WORKSPACE_ROOT") from None
        return str(self._container_root / relative)


__all__ = ["WorkspacePathError", "WorkspacePathMapper"]
