"""Immutable authorization for a bounded, ordered backlog execution."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from factory.domain.backlog import WorkItem

PIPELINE = (
    "Trigger:watch",
    "Conditions:active,ordered",
    "Validators:authorization,scope,dependencies,wip,duplicates,human_gate",
    "Actions:materialize_issue,pause,resume,cancel,request_human",
)
STOP_CONDITIONS = ("WAITING_HUMAN", "BLOCKED", "FAILED", "CANCELLED")


class SprintState(StrEnum):
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    CANCELLED = "CANCELLED"
    COMPLETE = "COMPLETE"


@dataclass(frozen=True, slots=True)
class SprintStep:
    item: WorkItem
    dependencies: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SprintManifest:
    sprint_id: str
    steps: tuple[SprintStep, ...]
    wip_limit: int = 1
    stop_conditions: tuple[str, ...] = STOP_CONDITIONS
    pipeline: tuple[str, ...] = PIPELINE

    def __post_init__(self) -> None:
        if not self.sprint_id or not self.sprint_id.isascii() or len(self.sprint_id) > 80:
            raise ValueError("invalid sprint id")
        if self.wip_limit != 1 or not self.steps:
            raise ValueError("sprint requires steps and WIP=1")
        if self.stop_conditions != STOP_CONDITIONS or self.pipeline != PIPELINE:
            raise ValueError("unsupported sprint policy or action")
