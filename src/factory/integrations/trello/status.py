"""Optional Trello card projection of the sanitized Factory status."""

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from factory.domain.models import StatusSnapshot


class TrelloStatusChannel:
    """Update one operator-owned card description on meaningful events only."""

    def __init__(
        self,
        card_id: str,
        key: str,
        token: str,
        *,
        send: Callable[[Request], None] | None = None,
    ) -> None:
        if not re.fullmatch(r"[A-Za-z0-9]+", card_id):
            raise ValueError("invalid Trello card id")
        self._card_id = card_id
        self._key = key
        self._token = token
        self._send = send or self._send_request

    def sync(self, snapshot: StatusSnapshot) -> None:
        """Send allowlisted fields only; credentials stay in the request body."""
        lines = [f"Factory: {snapshot.phase}"]
        for label, value in (
            ("Task", snapshot.task_id),
            ("Project", snapshot.project_id),
            ("Repository", snapshot.repository),
            ("Agent", snapshot.agent),
            ("Run", snapshot.run_id),
            ("Workspace", snapshot.workspace_id),
            ("Started", snapshot.started_at),
            ("Heartbeat", snapshot.last_heartbeat),
            ("Agent heartbeat", snapshot.agent_heartbeat),
            ("Agent liveness", snapshot.agent_liveness),
            ("Last transition", snapshot.last_transition),
            ("Finished", snapshot.finished_at),
            ("Evidence", snapshot.evidence),
            ("Blocked reason", snapshot.blocked_reason),
            ("Action", snapshot.action),
            ("Issue", snapshot.issue_url),
            ("Pull request", snapshot.pr_url),
            ("Commit", snapshot.commit_sha),
        ):
            if value is not None:
                lines.append(f"{label}: {value}")
        if snapshot.history:
            lines.extend(("", "Recent transitions:", *snapshot.history))
        request = Request(
            f"https://api.trello.com/1/cards/{self._card_id}",
            data=urlencode(
                {"key": self._key, "token": self._token, "desc": "\n".join(lines)}
            ).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="PUT",
        )
        self._send(request)

    def alert(self, snapshot: StatusSnapshot) -> None:
        """Comment on the card only for an actionable event."""
        if snapshot.phase not in {"WAITING_HUMAN", "FAILED", "STALLED", "DONE"}:
            return
        lines = [f"Factory alert: {snapshot.phase}"]
        if snapshot.issue_url:
            lines.append(snapshot.issue_url)
        if snapshot.pr_url:
            lines.append(snapshot.pr_url)
        if snapshot.evidence:
            lines.append(snapshot.evidence)
        request = Request(
            f"https://api.trello.com/1/cards/{self._card_id}/actions/comments",
            data=urlencode(
                {"key": self._key, "token": self._token, "text": "\n".join(lines)}
            ).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        self._send(request)

    @staticmethod
    def _send_request(request: Request) -> None:
        with urlopen(request, timeout=10) as response:  # noqa: S310 - fixed HTTPS host
            response.read(1)
