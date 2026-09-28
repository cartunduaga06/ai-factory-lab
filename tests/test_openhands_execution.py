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


# -- shared-workspace hook configuration -----------------------------------

HOOK_COMMAND = "python -m factory.integrations.workspace.shared_policy"


def test_no_hook_config_by_default() -> None:
    execution = OpenHandsExecution(agent_profile_id="p")
    assert execution.hook_payload() is None
    payload = build_creation_payload(_task(), WORKSPACE_PATH, execution).as_dict()
    assert "hook_config" not in payload


def test_hook_config_targets_file_editor_and_stop_synchronously() -> None:
    execution = OpenHandsExecution(agent_profile_id="p", shared_workspace_hook_command=HOOK_COMMAND)
    hook_config = execution.hook_payload()
    assert hook_config is not None
    post = hook_config["PostToolUse"]
    stop = hook_config["Stop"]
    assert isinstance(post, list) and isinstance(stop, list)
    assert post[0]["matcher"] == "file_editor"  # type: ignore[index]
    post_hook = post[0]["hooks"][0]  # type: ignore[index]
    stop_hook = stop[0]["hooks"][0]  # type: ignore[index]
    for hook in (post_hook, stop_hook):
        assert hook["type"] == "command"
        assert hook["command"] == HOOK_COMMAND
        # Synchronous: an async hook cannot normalize before the next step, and
        # an async Stop hook cannot block completion.
        assert hook["async"] is False
        assert hook["timeout"] >= 1


def test_hook_config_is_attached_to_the_creation_payload() -> None:
    payload = build_creation_payload(
        _task(),
        WORKSPACE_PATH,
        OpenHandsExecution(agent_profile_id="p", shared_workspace_hook_command=HOOK_COMMAND),
    ).as_dict()
    assert payload["hook_config"]["PostToolUse"][0]["matcher"] == "file_editor"  # type: ignore[index]


def test_blank_hook_command_is_rejected() -> None:
    with pytest.raises(ValueError):
        OpenHandsExecution(agent_profile_id="p", shared_workspace_hook_command="  ")


def test_non_positive_hook_timeout_is_rejected() -> None:
    with pytest.raises(ValueError):
        OpenHandsExecution(
            agent_profile_id="p",
            shared_workspace_hook_command=HOOK_COMMAND,
            hook_timeout=0,
        )


def test_hook_payload_matches_the_installed_hook_config_contract() -> None:
    # Validate the emitted payload against the real HookConfig when the SDK is
    # importable; skipped otherwise so the suite has no hard SDK dependency.
    hooks = pytest.importorskip("openhands.sdk.hooks.config")
    config = hooks.HookConfig.model_validate(
        OpenHandsExecution(
            agent_profile_id="p", shared_workspace_hook_command=HOOK_COMMAND
        ).hook_payload()
    )
    post = config.post_tool_use[0]
    assert post.matches("file_editor")
    assert [h.command for h in post.hooks] == [HOOK_COMMAND]
    assert [h.command for m in config.stop for h in m.hooks] == [HOOK_COMMAND]
    # Only exit code 2 blocks; the hook module returns exactly 2 on failure.
    assert post.hooks[0].async_ is False
