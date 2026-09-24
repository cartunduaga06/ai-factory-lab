"""Mapping between OpenHands execution states and factory run statuses.

OpenHands Agent Server reports a conversation's execution state as one of:
``idle``, ``running``, ``paused``, ``waiting_for_confirmation``, ``finished``,
``error``, ``stuck``, ``deleting``. The factory has exactly five run statuses
(``PENDING``, ``RUNNING``, ``SUCCEEDED``, ``FAILED``, ``CANCELLED``).

The mapping is explicit and total:

===================== ================== =====================================
OpenHands state       Factory RunStatus  Rationale
===================== ================== =====================================
``idle``              ``PENDING``        Created; no agent loop has run yet.
``running``           ``RUNNING``        Actively processing.
``paused``            ``RUNNING``        Interrupted but resumable, not done.
``waiting_for_confirmation`` ``RUNNING`` Awaiting input; still in flight.
``finished``          ``SUCCEEDED``      The run completed its task.
``error``             ``FAILED``         Terminal, not a success.
``stuck``             ``FAILED``         Terminal, the agent could not proceed.
``deleting``          ``CANCELLED``      Terminal; the execution is being torn down.
===================== ================== =====================================

Anything else raises :class:`~factory.integrations.openhands.client.OpenHandsStatusError`.
An unrecognized state is never coerced to a factory status — in particular it is
never silently mapped to ``SUCCEEDED``.
"""

from __future__ import annotations

from enum import StrEnum

from factory.domain.enums import RunStatus
from factory.integrations.openhands.client import OpenHandsStatusError


class OpenHandsExecutionStatus(StrEnum):
    """Execution states the supported OpenHands Agent Server can report."""

    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    WAITING_FOR_CONFIRMATION = "waiting_for_confirmation"
    FINISHED = "finished"
    ERROR = "error"
    STUCK = "stuck"
    DELETING = "deleting"


#: The single source of truth for the mapping. Keys are the states' string
#: values; ``StrEnum`` members hash and compare as their underlying value, so a
#: plain ``str`` lookup works without an ``Enum`` constructor that could raise.
_STATUS_MAP: dict[str, RunStatus] = {
    OpenHandsExecutionStatus.IDLE: RunStatus.PENDING,
    OpenHandsExecutionStatus.RUNNING: RunStatus.RUNNING,
    OpenHandsExecutionStatus.PAUSED: RunStatus.RUNNING,
    OpenHandsExecutionStatus.WAITING_FOR_CONFIRMATION: RunStatus.RUNNING,
    OpenHandsExecutionStatus.FINISHED: RunStatus.SUCCEEDED,
    OpenHandsExecutionStatus.ERROR: RunStatus.FAILED,
    OpenHandsExecutionStatus.STUCK: RunStatus.FAILED,
    OpenHandsExecutionStatus.DELETING: RunStatus.CANCELLED,
}

#: Factory statuses that end a run's life.
TERMINAL_RUN_STATUSES = frozenset({RunStatus.SUCCEEDED, RunStatus.FAILED, RunStatus.CANCELLED})


def map_status(raw: object) -> RunStatus:
    """Normalize an OpenHands execution status into a factory :class:`RunStatus`.

    Raises:
        OpenHandsStatusError: if ``raw`` is not one of the states this adapter
            knows about. Failing loudly is deliberate: an unrecognized state must
            not be guessed into ``SUCCEEDED``.

    The lookup never raises internally and the error is constructed outside any
    ``Enum``/provider exception context, so ``raw`` is never retained: the
    raised error has no message, attribute, ``__cause__`` or ``__context__``
    carrying the untrusted external value.
    """
    if isinstance(raw, str):
        status = _STATUS_MAP.get(raw)
        if status is not None:
            return status
    raise OpenHandsStatusError()


def is_terminal(run_status: RunStatus) -> bool:
    """Whether a factory run status is terminal."""
    return run_status in TERMINAL_RUN_STATUSES


__all__ = [
    "TERMINAL_RUN_STATUSES",
    "OpenHandsExecutionStatus",
    "is_terminal",
    "map_status",
]
