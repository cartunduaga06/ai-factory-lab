"""Deliver durable status events through replaceable notification channels."""

from __future__ import annotations

from typing import Protocol

from factory.domain.models import StatusSnapshot
from factory.orchestration.status import StatusService

ALERT_PHASES = frozenset({"WAITING_HUMAN", "FAILED", "STALLED", "DONE"})


class StatusEventStore(Protocol):
    """Durable queue of meaningful state changes."""

    def pending(self) -> list[tuple[str, str]]: ...

    def mark_delivered(self, event_id: str) -> None: ...

    def observe(self, snapshot: StatusSnapshot) -> None: ...


class StatusChannel(Protocol):
    """Destination for a current operator snapshot (Trello, another channel)."""

    def sync(self, snapshot: StatusSnapshot) -> None: ...


class AlertChannel(Protocol):
    """Optional alert destination; never called for heartbeats."""

    def alert(self, snapshot: StatusSnapshot) -> None: ...


class StatusEventPublisher:
    """Send each queued transition once, retaining it after delivery failure."""

    def __init__(
        self,
        service: StatusService,
        store: StatusEventStore,
        channel: StatusChannel,
        alert_channel: AlertChannel | None = None,
    ) -> None:
        self._service = service
        self._store = store
        self._channel = channel
        self._alert_channel = alert_channel

    def flush(self) -> None:
        """Observe timeout/recovery and deliver only persisted meaningful events."""
        self._store.observe(self._service.current())
        pending = self._store.pending()
        latest: dict[str, str] = {}
        for event_id, task_id in pending:
            latest[task_id] = event_id
        for task_id in latest:
            try:
                snapshot = self._service.for_task(task_id)
            except KeyError:
                for event_id, queued_task in pending:
                    if queued_task == task_id:
                        self._store.mark_delivered(event_id)
                continue
            self._channel.sync(snapshot)
            if snapshot.phase in ALERT_PHASES and self._alert_channel is not None:
                self._alert_channel.alert(snapshot)
            for event_id, queued_task in pending:
                if queued_task == task_id:
                    self._store.mark_delivered(event_id)
