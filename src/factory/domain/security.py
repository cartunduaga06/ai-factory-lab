"""Safe, immutable security review facts shared across the review boundary."""

from __future__ import annotations

from dataclasses import dataclass

SECURITY_RULE_VERSION = "factory-security-v1"


@dataclass(frozen=True, slots=True)
class SecurityFinding:
    """A rule match identified without retaining source text or a raw path."""

    rule_id: str
    path_digest: str
    line: int


@dataclass(frozen=True, slots=True)
class SecurityReview:
    """Result bound to the validated revision and versioned rule set."""

    rule_version: str
    revision: str
    findings: tuple[SecurityFinding, ...]

    @property
    def critical(self) -> bool:
        return bool(self.findings)
