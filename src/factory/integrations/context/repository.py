"""Deterministic local repository instructions and one relevant code or docs file."""

from __future__ import annotations

import hashlib
import re
import stat
from pathlib import Path

from factory.domain.context import ContextFragment, content_digest
from factory.domain.models import FactoryTask

_PATH = re.compile(r"(?<![\w/])(?:src|docs|tests)/[\w./-]+\.(?:py|md)(?![\w/])")
_MAX_FILE_BYTES = 1_000_000
_RENDER_CHARS = 8_000


class RepositoryContextSource:
    """Read regular files from the configured checkout, with full-byte identity."""

    required = True

    def __init__(self, checkout: str) -> None:
        self._root = Path(checkout)

    def fragments(self, task: FactoryTask) -> tuple[ContextFragment, ...]:
        instructions = self._fragment("AGENTS.md", "instructions")
        relevant = self._choose_file(task)
        return (instructions, self._fragment(relevant, "repository"))

    def _choose_file(self, task: FactoryTask) -> str:
        mentioned: list[str] = _PATH.findall(task.title + "\n" + task.body)
        if mentioned:
            path = sorted(set(mentioned))[0]
            if not self._is_regular(path):
                raise ValueError("mentioned repository context unavailable")
            return path
        for path in ("README.md", "docs/architecture.md"):
            if self._is_regular(path):
                return path
        for candidate in sorted(self._root.glob("src/**/*.py")):
            relative = candidate.relative_to(self._root).as_posix()
            if self._is_regular(relative):
                return relative
        raise ValueError("relevant repository context unavailable")

    def _is_regular(self, relative: str) -> bool:
        if any(part in {"", ".", ".."} for part in Path(relative).parts):
            return False
        path = self._root / relative
        try:
            return stat.S_ISREG(path.lstat().st_mode)
        except OSError:
            return False

    def _fragment(self, relative: str, kind: str) -> ContextFragment:
        if not self._is_regular(relative):
            raise ValueError("repository context unavailable")
        path = self._root / relative
        data = path.read_bytes()
        if not data or len(data) > _MAX_FILE_BYTES:
            raise ValueError("repository context invalid or over budget")
        decoded = data.decode("utf-8")
        content = f"Repository file {relative}:\n{decoded[:_RENDER_CHARS]}"
        return ContextFragment(
            "repository",
            kind,
            relative,
            hashlib.sha256(data).hexdigest(),
            content_digest(content),
            content,
        )
