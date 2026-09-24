"""Tests for translating a factory task into an OpenHands request.

Covers the instruction derivation (title/body/target repository) and the
conversation-creation payload (workspace path, agent selection, instruction).
"""

from __future__ import annotations

import pytest

from factory.domain.models import FactoryTask, TaskSource
from factory.integrations.openhands.execution import (
    MAX_INSTRUCTION_CHARS,
    ConversationPayload,
    OpenHandsExecution,
    build_creation_payload,
    build_instruction,
)

WORKSPACE_PATH = "/var/lib/factory/workspaces/task-1"
SECRET = "sk-super-secret-value"


def _task(**overrides: object) -> FactoryTask:
    defaults: dict[str, object] = {
        "title": "Add a health endpoint",
        "target_repository": "cartunduaga06/finanza-ia",
        "source": TaskSource("github", "cartunduaga06/ai-factory-lab", 42),
        "body": "Expose GET /health returning 200.",
    }
    defaults.update(overrides)
    return FactoryTask(**defaults)  # type: ignore[arg-type]


# -- instruction -----------------------------------------------------------


def test_instruction_carries_title_body_and_target() -> None:
    instruction = build_instruction(_task())
    assert "Add a health endpoint" in instruction
    assert "cartunduaga06/finanza-ia" in instruction
    assert "Expose GET /health returning 200." in instruction
    assert "cartunduaga06/ai-factory-lab#42" in instruction


def test_instruction_states_the_hard_boundaries() -> None:
    instruction = build_instruction(_task())
    assert "Never merge a pull request." in instruction
    assert "Never push directly to a default branch" in instruction
    assert "Never deploy anything" in instruction


def test_instruction_handles_a_missing_body() -> None:
    instruction = build_instruction(_task(body="   "))
    assert "(no description provided)" in instruction


def test_instruction_is_bounded() -> None:
    instruction = build_instruction(_task(body="x" * (MAX_INSTRUCTION_CHARS * 2)))
    assert len(instruction) <= MAX_INSTRUCTION_CHARS + 40
    assert "truncated by AI Factory Lab" in instruction


# -- execution profile -----------------------------------------------------


def test_profile_and_settings_are_mutually_exclusive() -> None:
    with pytest.raises(ValueError):
        OpenHandsExecution()
    with pytest.raises(ValueError):
        OpenHandsExecution(agent_profile_id="p", agent_settings={"agent_kind": "openhands"})


def test_profile_execution_selects_the_profile() -> None:
    execution = OpenHandsExecution(agent_profile_id="84dd09d2")
    assert execution.agent_payload() == {"agent_profile_id": "84dd09d2"}


def test_settings_execution_forwards_settings_and_encrypted_flag() -> None:
    execution = OpenHandsExecution(
        agent_settings={"agent_kind": "openhands", "llm": {"model": "m"}},
        secrets_encrypted=True,
    )
    payload = execution.agent_payload()
    assert payload["agent_settings"] == {"agent_kind": "openhands", "llm": {"model": "m"}}
    assert payload["secrets_encrypted"] is True


def test_non_positive_max_iterations_is_rejected() -> None:
    with pytest.raises(ValueError):
        OpenHandsExecution(agent_profile_id="p", max_iterations=0)


def test_execution_secret_values_are_collected_for_redaction() -> None:
    execution = OpenHandsExecution(
        agent_settings={
            "llm": {"api_key": SECRET, "model": "m"},
            "nested": {"inner": {"token": "t"}},
        }
    )
    assert SECRET in execution.secret_values()
    assert "t" in execution.secret_values()


# -- payload ---------------------------------------------------------------


def test_payload_uses_the_supplied_workspace_path() -> None:
    payload = build_creation_payload(
        _task(), WORKSPACE_PATH, OpenHandsExecution(agent_profile_id="p")
    ).as_dict()
    assert payload["workspace"] == {"kind": "LocalWorkspace", "working_dir": WORKSPACE_PATH}


def test_payload_does_not_run_work_on_the_factory_checkout() -> None:
    # The engine must operate where the factory decided, never its own default.
    payload = build_creation_payload(
        _task(), WORKSPACE_PATH, OpenHandsExecution(agent_profile_id="p")
    ).as_dict()
    assert "working_dir" in payload["workspace"]  # type: ignore[operator]


def test_payload_sends_the_instruction_and_runs_it() -> None:
    payload = build_creation_payload(
        _task(), WORKSPACE_PATH, OpenHandsExecution(agent_profile_id="p")
    ).as_dict()
    message = payload["initial_message"]
    assert isinstance(message, dict)
    assert message["run"] is True
    assert message["role"] == "user"
    content = message["content"]
    assert isinstance(content, list)
    assert "Add a health endpoint" in content[0]["text"]


def test_payload_carries_no_confirmation_by_default() -> None:
    # A factory run must not stall waiting for an interactive confirmation.
    payload = build_creation_payload(
        _task(), WORKSPACE_PATH, OpenHandsExecution(agent_profile_id="p")
    ).as_dict()
    assert payload["confirmation_policy"] == {"kind": "NeverConfirm"}


def test_conversation_payload_returns_a_copy() -> None:
    payload = ConversationPayload(body={"a": 1})
    copied = payload.as_dict()
    copied["b"] = 2
    assert payload.as_dict() == {"a": 1}
