"""Assemble the required task instruction and optional approved guidance."""

from __future__ import annotations

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
        content = (
            "You are executing a task dispatched by AI Factory Lab.\n\n"
            f"Target repository: {task.target_repository}\n"
            f"Task reference: {task.external_ref or task.task_id}\n"
            f"Task title: {task.title}\n\nTask description:\n{body[:8000]}\n\n" + _BOUNDARIES
        )
        return (
            ContextFragment("factory", "task", task.task_id, "1", content_digest(content), content),
        )


class ContextPackBuilder:
    """Build a deterministic pack from mandatory task and injected sources."""

    def __init__(self, sources: tuple[ContextSource, ...] = (), budget: int = 32_000) -> None:
        self._sources = (TaskContextSource(), *sources)
        self._budget = budget

    def build(
        self,
        task: FactoryTask,
        *,
        feedback: str | None = None,
        previous: ContextPack | None = None,
    ) -> ContextPack:
        fragments = tuple(
            fragment for source in self._sources for fragment in source.fragments(task)
        )
        if feedback is not None:
            if previous is None:
                raise ValueError("rework requires historical context pack")
            base = build_pack(fragments, budget=self._budget)
            if base.sha256 != (previous.base_sha256 or previous.sha256):
                raise ValueError("historical context pack changed")
            text = f"QA feedback for this rework:\n{feedback}"
            fragments += (
                ContextFragment(
                    "factory", "feedback", previous.sha256, "1", content_digest(text), text
                ),
            )
        return build_pack(
            fragments,
            budget=self._budget,
            base_sha256=(previous.base_sha256 or previous.sha256) if previous else None,
        )
