"""Assemble the required task instruction and optional approved guidance."""

from __future__ import annotations

import json

from factory.domain.context import (
    ContextFragment,
    ContextPack,
    ContextSource,
    build_pack,
    content_digest,
)
from factory.domain.models import FactoryTask

_BOUNDARIES = (
    "Hard boundaries:\n"
    "- Work only inside the workspace you were given.\n"
    "- Never push directly to a default branch (main/master).\n"
    "- Never merge a pull request.\n"
    "- Never deploy anything and never modify hosts, containers or secrets.\n"
    "- Do not modify any repository other than the target repository.\n\n"
    "When you are done, reply with a short summary of what you changed or found."
)


class TaskContextSource:
    """Produce the mandatory bounded issue instruction without external I/O."""

    def fragments(self, task: FactoryTask) -> tuple[ContextFragment, ...]:
        body = task.body.strip() or "(no description provided)"
        source_id = (
            f"{task.source.provider}:{task.source.repository_slug}#{task.source.issue_number}"
            if task.source
            else task.task_id
        )
        content = (
            "You are executing a task dispatched by AI Factory Lab.\n\n"
            + (f"Project ID: {task.project_id}\n" if task.project_id != "ai-factory-lab" else "")
            + f"Target repository: {task.target_repository}\n"
            f"Task reference: {task.external_ref or task.task_id}\n"
            f"Task title: {task.title}\n\nTask description:\n{body[:8000]}\n\n" + _BOUNDARIES
        )
        # The prompt is bounded, while identity covers every authoritative field.
        identity = json.dumps(
            {
                "target_repository": task.target_repository,
                **({"project_id": task.project_id} if task.project_id != "ai-factory-lab" else {}),
                "source": (
                    [task.source.provider, task.source.repository_slug, task.source.issue_number]
                    if task.source
                    else None
                ),
                "local_task_id": task.task_id if task.source is None else None,
                "title": task.title,
                "body": task.body,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return (
            ContextFragment(
                "factory",
                "task",
                source_id,
                content_digest(identity),
                content_digest(content),
                content,
            ),
        )


class ContextBuildError(ValueError):
    """A mandatory source could not produce a valid, bounded pack."""


class ContextPackBuilder:
    """Build a deterministic pack from mandatory task and injected sources."""

    def __init__(self, sources: tuple[ContextSource, ...] = (), budget: int = 48_000) -> None:
        self._sources = (TaskContextSource(), *sources)
        self._budget = budget

    def build(
        self,
        task: FactoryTask,
        *,
        feedback: str | None = None,
        previous: ContextPack | None = None,
    ) -> ContextPack:
        try:
            parts: list[ContextFragment] = []
            for source in self._sources:
                supplied = source.fragments(task)
                if not supplied and getattr(source, "required", True):
                    raise ValueError("mandatory source returned no fragments")
                parts.extend(supplied)
            fragments = tuple(parts)
        except Exception:  # noqa: BLE001 - source messages may contain private data
            raise ContextBuildError("required context unavailable") from None
        if feedback is not None:
            if previous is None:
                raise ValueError("rework requires historical context pack")
            try:
                base = build_pack(fragments, budget=self._budget)
            except ValueError:
                raise ContextBuildError("required context invalid or over budget") from None
            if base.sha256 != (previous.base_sha256 or previous.sha256):
                raise ValueError("historical context pack changed")
            text = f"QA feedback for this rework:\n{feedback}"
            fragments += (
                ContextFragment(
                    "factory", "feedback", previous.sha256, "1", content_digest(text), text
                ),
            )
        try:
            return build_pack(
                fragments,
                budget=self._budget,
                base_sha256=(previous.base_sha256 or previous.sha256) if previous else None,
            )
        except ValueError:
            raise ContextBuildError("required context invalid or over budget") from None
