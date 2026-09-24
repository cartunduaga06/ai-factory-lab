"""Status mapping tests: external OpenHands state -> factory ``RunStatus``.

The mapping is the safety-critical part of the adapter. Every recognized state
is asserted, and an unrecognized one must fail rather than be guessed into
success.
"""

from __future__ import annotations

import traceback

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


def test_status_error_does_not_expose_a_secret_bearing_status() -> None:
    secret = "ghp_supersecret123"
    raw = f"unexpected-{secret}"

    with pytest.raises(OpenHandsStatusError) as caught:
        map_status(raw)

    error = caught.value
    # Neither the message nor its repr may carry the external value.
    assert secret not in str(error)
    assert secret not in repr(error)
    assert raw not in str(error)
    assert raw not in repr(error)
    # No chained provider/Enum exception may keep the raw value reachable.
    assert error.__cause__ is None
    assert error.__context__ is None
    # A formatted traceback must be equally clean.
    assert secret not in "".join(traceback.format_exception(error))
    # The raw external value must not be retained anywhere on the error.
    assert not hasattr(error, "raw")
    assert all(secret not in repr(value) for value in vars(error).values())


def test_status_error_sanitizes_a_non_string_malicious_payload() -> None:
    secret = "ghp_supersecret123"

    class _Payload:
        def __init__(self) -> None:
            self.token = secret

        def __repr__(self) -> str:
            return f"Payload(token={self.token!r})"

        def __str__(self) -> str:
            return self.token

    with pytest.raises(OpenHandsStatusError) as caught:
        map_status(_Payload())

    error = caught.value
    assert secret not in str(error)
    assert secret not in repr(error)
    assert error.__cause__ is None
    assert error.__context__ is None
    assert secret not in "".join(traceback.format_exception(error))
    assert not hasattr(error, "raw")
    assert all(secret not in repr(value) for value in vars(error).values())


def test_unknown_status_with_secret_never_maps_to_succeeded() -> None:
    with pytest.raises(OpenHandsStatusError) as caught:
        map_status("unexpected-ghp_supersecret123")
    assert not isinstance(caught.value, RunStatus)


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
