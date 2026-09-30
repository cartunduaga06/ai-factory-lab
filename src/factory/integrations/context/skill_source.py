"""Approved pinned ECC guidance exposed through the neutral context source port."""

from __future__ import annotations

from factory.domain.context import ContextFragment, content_digest
from factory.domain.enums import TaskKind
from factory.domain.models import FactoryTask
from factory.integrations.context.skill_registry import (
    load_registered_skill,
    load_registry,
    select_skill,
)


class ApprovedSkillSource:
    """Read only the pinned registry and digest-verified approved skill."""

    def __init__(self, selection: str | None) -> None:
        self._selection = selection
        self.required = selection is not None

    def fragments(self, task: FactoryTask) -> tuple[ContextFragment, ...]:
        if self._selection is None or task.kind is not TaskKind.CODE:
            return ()
        records = load_registry()
        if self._selection == "auto":
            selected = select_skill(task, records)
        else:
            matches = [record for record in records if record.name == self._selection]
            if len(matches) != 1 or matches[0].approval_status != "APPROVED":
                raise ValueError("unsupported ECC skill")
            selected = matches[0]
        guidance = load_registered_skill(selected)
        content = (
            f"Optional review guidance from pinned ECC {selected.name}. "
            "Use only applicable checks. Factory quality gates and human review "
            "remain authoritative. Do not enable hooks or deploy.\n\n" + guidance
        )
        return (
            ContextFragment(
                "ecc",
                "skill",
                selected.name,
                selected.upstream_commit,
                content_digest(content),
                content,
            ),
        )
