"""Project checked Trello card lifecycle projection."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any, Protocol

from factory.domain.feedback import FeedbackIdentity
from factory.domain.ports import WorkItemFeedbackSink
from factory.domain.projects import ProjectRegistry, ProjectRoutingError

_START = "<!-- factory-feedback:start -->"
_END = "<!-- factory-feedback:end -->"
_PHASES = {"WAITING_HUMAN", "MERGED", "CLOSED", "FAILED", "DONE"}


def source_description(description: str) -> str:
    """Remove one complete Factory feedback block from a card description."""
    start_count = description.count(_START)
    end_count = description.count(_END)
    if start_count == 0 and end_count == 0:
        return description
    if start_count != 1 or end_count != 1:
        raise TrelloFeedbackError("invalid Trello feedback markers")
    prefix, _, remainder = description.partition(_START)
    block, marker, trailing = remainder.partition(_END)
    if not marker or _START in block or _END in block:
        raise TrelloFeedbackError("invalid Trello feedback markers")
    if not prefix.endswith("\n\n") or not block.startswith("\n"):
        raise TrelloFeedbackError("invalid Trello feedback block")
    # Trello users may append text after the Factory block. Preserve that text
    # as source content while removing only the owned block on the next write.
    return prefix[:-2] + trailing


class TrelloFeedbackTransport(Protocol):
    def request_json(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> Any:  # noqa: ANN401
        """Read or update exactly one card."""


class UrllibFeedbackTransport:
    def request_json(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> Any:  # noqa: ANN401
        request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            raise TrelloFeedbackError("Trello feedback request failed") from None


class TrelloFeedbackError(RuntimeError):
    """Sanitized card identity or write failure."""


class TrelloWorkItemFeedbackSink(WorkItemFeedbackSink):
    def __init__(
        self,
        key: str,
        token: str,
        registry: ProjectRegistry,
        transport: TrelloFeedbackTransport | None = None,
        *,
        done_list_id: str | None = None,
    ) -> None:
        self._key = key
        self._token = token
        self._registry = registry
        if done_list_id is not None and re.fullmatch(r"[A-Za-z0-9]+", done_list_id) is None:
            raise TrelloFeedbackError("invalid DONE list identity")
        self._done_list_id = done_list_id
        self._transport = transport or UrllibFeedbackTransport()

    def sync(self, identity: FeedbackIdentity, phase: str) -> None:
        if phase not in _PHASES or identity.work_item_provider != "trello":
            raise TrelloFeedbackError("invalid card feedback phase or provider")
        if (
            identity.work_item_id is None
            or re.fullmatch(r"[A-Za-z0-9]+", identity.work_item_id) is None
        ):
            raise TrelloFeedbackError("invalid card identity")
        try:
            self._registry.resolve(identity.project_id, identity.repository_slug)
        except ProjectRoutingError:
            raise TrelloFeedbackError("card project routing mismatch") from None
        url = f"https://api.trello.com/1/cards/{identity.work_item_id}"
        headers = {
            "Authorization": f'OAuth oauth_consumer_key="{self._key}", oauth_token="{self._token}"',
            "Accept": "application/json",
        }
        try:
            card = self._transport.request_json(
                "GET", url + "?fields=id,desc,dueComplete,idList", headers, None
            )
        except Exception:
            raise TrelloFeedbackError("Trello card read failed") from None
        if not isinstance(card, Mapping) or card.get("id") != identity.work_item_id:
            raise TrelloFeedbackError("Trello card identity mismatch")
        raw = card.get("desc")
        if not isinstance(raw, str):
            raise TrelloFeedbackError("invalid Trello card description")
        original = source_description(raw)
        declarations = re.findall(r"(?im)^project_id\s*[:=]\s*([^\s]+)\s*$", original)
        if len(declarations) > 1 or (declarations and declarations[0] != identity.project_id):
            raise TrelloFeedbackError("Trello card project identity mismatch")
        if not declarations:
            # The persisted, registry-validated feedback identity is authoritative
            # for a linked card whose historical description lacks its marker.
            original = f"project_id: {identity.project_id}\n{original}"
        suffix = (
            f"{_START}\nFactory: {phase}\nProject: {identity.project_id}\n"
            f"Sprint: {identity.sprint_id or '-'}\n"
            f"Issue: {identity.repository_slug}#{identity.issue_number}\n"
            f"Task: {identity.task_id}\nRun: {identity.run_id}\n"
            f"Workspace: {identity.workspace_id}\nCommit: {identity.commit_sha}\n"
            f"PR: {identity.repository_slug}#{identity.pull_request_number}\n{_END}"
        )
        description = original + "\n\n" + suffix
        completed = phase == "DONE"
        desired_list = self._done_list_id if completed else None
        if (
            raw == description
            and card.get("dueComplete") is completed
            and (desired_list is None or card.get("idList") == desired_list)
        ):
            return
        update = {"desc": description, "dueComplete": completed}
        if desired_list is not None:
            update["idList"] = desired_list
        payload = json.dumps(update).encode()
        headers = {**headers, "Content-Type": "application/json"}
        try:
            updated = self._transport.request_json("PUT", url, headers, payload)
        except Exception:
            raise TrelloFeedbackError("Trello card update failed") from None
        if (
            not isinstance(updated, Mapping)
            or updated.get("id") != identity.work_item_id
            or updated.get("desc") != description
            or updated.get("dueComplete") is not completed
            or (desired_list is not None and updated.get("idList") != desired_list)
        ):
            raise TrelloFeedbackError("Trello card update identity mismatch")
