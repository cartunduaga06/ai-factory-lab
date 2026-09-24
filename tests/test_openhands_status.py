"""Status mapping tests: external OpenHands state -> factory ``RunStatus``.

The mapping is the safety-critical part of the adapter. Every recognized state
is asserted, and an unrecognized one must fail rather than be guessed into
success.
"""

from __future__ import annotations

import pytest

from factory.domain.enums import RunStatus
from factory.integrations.openhands.client import OpenHandsStatusError
from factory.integrations.openhands.status import (
    TERMINAL_RUN_STATUSES,
    OpenHandsExecutionStatus,
    is_terminal,
    map_status,
)


@pytest.mark.parametrize(
    ("external", "expected"),
    [
        ("idle", RunStatus.PENDING),
        ("running", RunStatus.RUNNING),
        ("paused", RunStatus.RUNNING),
        ("waiting_for_confirmation", RunStatus.RUNNING),
        ("finished", RunStatus.SUCCEEDED),
        ("error", RunStatus.FAILED),
        ("stuck", RunStatus.FAILED),
        ("deleting", RunStatus.CANCELLED),
    ],
)
def test_every_known_state_maps(external: str, expected: RunStatus) -> None:
    assert map_status(external) is expected


def test_mapping_covers_every_declared_state() -> None:
    # The enum and the mapping table must not drift apart.
    for state in OpenHandsExecutionStatus:
        assert map_status(state.value) is not None


@pytest.mark.parametrize(
    "external",
    ["queued", "success", "failed", "", "RUNNING", "unknown", None, 3, ["idle"]],
)
def test_unknown_state_fails_safely(external: object) -> None:
    with pytest.raises(OpenHandsStatusError):
        map_status(external)


def test_unknown_state_never_becomes_success() -> None:
    # An unrecognized state must not silently map to SUCCEEDED (or any status).
    with pytest.raises(OpenHandsStatusError):
        map_status("completed")


def test_terminal_classification_matches_factory_semantics() -> None:
    assert is_terminal(RunStatus.SUCCEEDED)
    assert is_terminal(RunStatus.FAILED)
    assert is_terminal(RunStatus.CANCELLED)
    assert not is_terminal(RunStatus.PENDING)
    assert not is_terminal(RunStatus.RUNNING)
    assert {
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLED,
    } == TERMINAL_RUN_STATUSES
