"""Translation of a factory task into an OpenHands execution request.

Two things are defined here, both provider-specific and therefore kept out of
the domain and orchestration layers:

* :class:`OpenHandsExecution` — how the agent that runs the task is configured.
  It mirrors the agent-server's own contract: a conversation is created either
  from a server-side agent profile (``agent_profile_id``) or from an inline
  ``agent_settings`` object. The factory never invents an LLM credential; it
  either defers to a profile the server owns or forwards settings that were
  configured for it out of band.
* :func:`build_instruction` — the bounded, deterministic prompt derived from a
  :class:`~factory.domain.models.FactoryTask`.

The instruction states the task and the factory's hard boundaries. It is a
request, not an authority: those boundaries are enforced by the factory and by
the environment, not by the agent's willingness to obey a prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from factory.domain.models import FactoryTask

#: Hard cap on the derived instruction, so an unusually long issue body cannot
#: turn a dispatch into a multi-megabyte request.
MAX_INSTRUCTION_CHARS = 8_000

#: Default iteration budget for a factory-dispatched conversation.
DEFAULT_MAX_ITERATIONS = 500

_SPRINT = "OpenHands Agent Server"


def _bound(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return f"{text[:limit]}\n\n[truncated by AI Factory Lab]"


def build_instruction(task: FactoryTask) -> str:
    """Derive the bounded implementation instruction for ``task``.

    Built only from factory-domain fields (title, body, target repository), so
    the prompt cannot smuggle in provider-specific or credential-bearing data.
    """
    body = task.body.strip() or "(no description provided)"
    reference = task.external_ref or task.task_id
    instruction = (
        f"You are executing a task dispatched by AI Factory Lab ({_SPRINT}).\n\n"
        f"Target repository: {task.target_repository}\n"
        f"Task reference: {reference}\n"
        f"Task title: {task.title}\n\n"
        f"Task description:\n{body}\n\n"
        "Hard boundaries:\n"
        "- Work only inside the workspace you were given.\n"
        "- Never push directly to a default branch (main/master).\n"
        "- Never merge a pull request.\n"
        "- Never deploy anything and never modify hosts, containers or secrets.\n"
        "- Do not modify any repository other than the target repository.\n\n"
        "When you are done, reply with a short summary of what you changed or "
        "found. If the task cannot be completed, say so plainly and explain why."
    )
    return _bound(instruction, MAX_INSTRUCTION_CHARS)


@dataclass(slots=True, frozen=True)
class OpenHandsExecution:
    """How the agent for a dispatched conversation is configured.

    Exactly one of ``agent_profile_id`` or ``agent_settings`` must be supplied,
    matching the agent-server contract. ``agent_profile_id`` is preferred: the
    server resolves the LLM and its credential itself, so no credential is ever
    sent by the factory.

    ``secrets_encrypted`` mirrors the server's flag for round-tripping settings
    whose credential fields are already cipher-encrypted server-side; the
    factory never stores or logs the plaintext in that case.
    """

    agent_profile_id: str | None = None
    agent_settings: Mapping[str, object] | None = None
    secrets_encrypted: bool = False
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    stuck_detection: bool = True
    autotitle: bool = True

    def __post_init__(self) -> None:
        if (self.agent_profile_id is None) == (self.agent_settings is None):
            raise ValueError("exactly one of agent_profile_id or agent_settings must be provided")
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be positive")

    def agent_payload(self) -> dict[str, object]:
        """The agent-selection fields to merge into the creation request."""
        if self.agent_profile_id is not None:
            return {"agent_profile_id": self.agent_profile_id}
        return {
            "agent_settings": dict(self.agent_settings or {}),
            "secrets_encrypted": self.secrets_encrypted,
        }

    def secret_values(self) -> tuple[str, ...]:
        """Credential-looking string values inside the settings, for redaction.

        Only used to scrub text leaving the integration; it is never logged or
        serialized. Nested structures are walked to a fixed depth so a malformed
        mapping cannot turn this into unbounded work.
        """
        found: list[str] = []
        _collect_secrets(self.agent_settings, found, depth=0)
        return tuple(found)


def _collect_secrets(value: object, found: list[str], depth: int) -> None:
    if depth > 6:
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and _looks_secret(key):
                if isinstance(item, str) and item:
                    found.append(item)
            else:
                _collect_secrets(item, found, depth + 1)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_secrets(item, found, depth + 1)


def _looks_secret(key: str) -> bool:
    lowered = key.lower()
    return any(token in lowered for token in ("api_key", "apikey", "secret", "token", "password"))


@dataclass(slots=True, frozen=True)
class ConversationPayload:
    """A fully-formed conversation-creation request."""

    body: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return dict(self.body)


def build_creation_payload(
    task: FactoryTask,
    workspace_path: str,
    execution: OpenHandsExecution,
) -> ConversationPayload:
    """Assemble the agent-server request for one factory task.

    ``workspace_path`` is the path the factory chose for the run's isolated
    workspace; it is passed through verbatim so the engine operates where the
    factory decided, not where the engine would default to.
    """
    body: dict[str, object] = {
        "workspace": {"kind": "LocalWorkspace", "working_dir": workspace_path},
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": execution.max_iterations,
        "stuck_detection": execution.stuck_detection,
        "autotitle": execution.autotitle,
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": build_instruction(task)}],
            "run": True,
        },
    }
    body.update(execution.agent_payload())
    return ConversationPayload(body=body)


__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "MAX_INSTRUCTION_CHARS",
    "ConversationPayload",
    "OpenHandsExecution",
    "build_creation_payload",
    "build_instruction",
]
