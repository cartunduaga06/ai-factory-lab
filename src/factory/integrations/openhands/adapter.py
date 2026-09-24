"""OpenHands Agent Server as a factory :class:`~factory.domain.models.AgentAdapter`.

OpenHands is the *execution engine* only. This adapter translates a
:class:`~factory.domain.models.FactoryTask` into an OpenHands conversation and
normalizes that conversation's state back into an
:class:`~factory.domain.models.AgentRun`. It owns no orchestration: it cannot
create issues, pick tasks, merge, deploy or advance the factory lifecycle.

Run identity
------------

The OpenHands conversation id *is* the factory ``AgentRun.run_id``. The mapping
is one-to-one and needs no provider-specific domain field:

=========================== ==================================================
Factory concept             OpenHands concept
=========================== ==================================================
``AgentRun.run_id``         conversation ``id``
``AgentRun.status``         ``execution_status`` via :func:`map_status`
``AgentRun.workspace.path`` ``workspace.working_dir``
``AgentRun.summary``        ``agent_final_response`` text, redacted and bounded
``AgentRun.started_at``     set when the conversation is created
``AgentRun.finished_at``    set only once the mapped status is terminal
=========================== ==================================================

Status mapping lives in :mod:`.status`; the translation of a task into an
execution request lives in :mod:`.execution`. This module is only the seam.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from factory.domain.enums import AgentKind
from factory.domain.models import AgentRun, FactoryTask, Workspace
from factory.integrations.base import AgentAdapterBase
from factory.integrations.openhands.client import (
    MAX_DETAIL_CHARS,
    OpenHandsClient,
    OpenHandsError,
    bound,
    redact,
)
from factory.integrations.openhands.execution import (
    OpenHandsExecution,
    build_creation_payload,
)
from factory.integrations.openhands.status import is_terminal, map_status

logger = logging.getLogger(__name__)

#: The one field the adapter needs from a conversation descriptor.
_EXECUTION_STATUS_KEY = "execution_status"
_CONVERSATION_ID_KEY = "id"


class OpenHandsAdapter(AgentAdapterBase):
    """Drives OpenHands Agent Server conversations as factory agent runs."""

    def __init__(
        self,
        client: OpenHandsClient,
        execution: OpenHandsExecution,
    ) -> None:
        self._client = client
        self._execution = execution

    def __repr__(self) -> str:
        # The client's repr is already credential-free, and ``execution`` holds
        # no plaintext credential field it would print.
        return f"OpenHandsAdapter(client={self._client!r})"

    @property
    def kind(self) -> AgentKind:
        return AgentKind.OPENHANDS

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, task: FactoryTask, workspace: Workspace) -> AgentRun:
        """Create the OpenHands conversation for ``task`` and return its run.

        The workspace path supplied by the factory is used verbatim as the
        conversation's working directory. The returned run carries the
        conversation id as its ``run_id`` so it can later be collected or
        cancelled exactly.

        Raises:
            OpenHandsError: a sanitized integration error if the conversation
                could not be created.
        """
        payload = build_creation_payload(task, workspace.path, self._execution).as_dict()
        descriptor = self._client.create_conversation(payload)
        conversation_id = self._conversation_id(descriptor)
        status = map_status(descriptor.get(_EXECUTION_STATUS_KEY))
        return AgentRun(
            task_id=task.task_id,
            adapter=self.kind,
            run_id=conversation_id,
            status=status,
            workspace=workspace,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC) if is_terminal(status) else None,
        )

    # -- collect -----------------------------------------------------------

    def collect(self, run: AgentRun) -> AgentRun:
        """Refresh ``run`` from the OpenHands conversation it points at.

        Only sanitized data crosses back: the mapped status, a redacted and
        bounded final-response summary, and ``finished_at`` for terminal states.
        No raw conversation payload is attached to the run.

        Raises:
            OpenHandsError: if the execution cannot be located or queried.
        """
        descriptor = self._client.get_conversation(run.run_id)
        if descriptor is None:
            raise OpenHandsError(f"OpenHands conversation {run.run_id} could not be located")

        status = map_status(descriptor.get(_EXECUTION_STATUS_KEY))
        run.status = status
        if is_terminal(status):
            run.finished_at = run.finished_at or datetime.now(UTC)
            run.summary = self._final_summary(run.run_id)
        else:
            run.finished_at = None
        return run

    # -- cancel ------------------------------------------------------------

    def cancel(self, run: AgentRun) -> None:
        """Request an interrupt for the run's conversation. Idempotent.

        A run that is already terminal in the factory's own view is left alone:
        there is nothing to interrupt, and re-issuing the request would add a
        pointless remote call. Otherwise the interrupt is requested once; a
        ``404`` (the conversation is already gone) is treated as already
        cancelled rather than a failure. Any other API error is raised, never
        converted into a silent success.
        """
        if run.is_terminal:
            return
        self._client.interrupt_conversation(run.run_id)

    # -- internals ---------------------------------------------------------

    @staticmethod
    def _conversation_id(descriptor: object) -> str:
        if not isinstance(descriptor, dict):
            raise OpenHandsError("OpenHands conversation creation returned no descriptor")
        value = descriptor.get(_CONVERSATION_ID_KEY)
        if not isinstance(value, str) or not value:
            raise OpenHandsError("OpenHands conversation creation returned no id")
        return value

    def _final_summary(self, conversation_id: str) -> str | None:
        """Fetch, redact and bound the agent's final response.

        Returns ``None`` rather than raising if the response cannot be read, so a
        terminal status is still reported. The text is scrubbed of every
        credential this integration holds and of common token shapes before it
        is allowed into the domain.
        """
        text = self._client.agent_final_response(conversation_id)
        if text is None:
            return None
        secrets = (*self._client.secret_values(), *self._execution.secret_values())
        return bound(redact(text, secrets), MAX_DETAIL_CHARS)


__all__ = ["OpenHandsAdapter"]
