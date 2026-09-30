"""Trello Sprint cards as provider-neutral backlog snapshots."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from factory.domain.backlog import WorkItem
from factory.domain.ports import BacklogSource
from factory.domain.projects import ProjectRegistry, ProjectRoutingError
from factory.integrations.trello.feedback import source_description


class TrelloBacklogError(RuntimeError):
    """Sanitized Trello read failure; no provider body or credential escapes."""


class TrelloTransport(Protocol):
    def get_json(self, url: str, headers: Mapping[str, str]) -> Any:  # noqa: ANN401
        """Return decoded JSON for one authenticated GET."""


class UrllibTrelloTransport:
    def get_json(self, url: str, headers: Mapping[str, str]) -> Any:  # noqa: ANN401
        request = urllib.request.Request(url, headers=dict(headers), method="GET")
        try:
            with urllib.request.urlopen(request, timeout=15) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError):
            raise TrelloBacklogError("Trello read failed") from None


class TrelloBacklogSource(BacklogSource):
    """Read Sprint cards and checked dependency checklist items, failing closed."""

    def __init__(
        self,
        key: str,
        token: str,
        sprint_list_id: str,
        ready_label_id: str,
        target_repository: str,
        transport: TrelloTransport | None = None,
        registry: ProjectRegistry | None = None,
    ) -> None:
        if not all(
            re.fullmatch(r"[A-Za-z0-9]+", value) for value in (sprint_list_id, ready_label_id)
        ):
            raise ValueError("invalid Trello list or label id")
        self._key = key
        self._token = token
        self._list = sprint_list_id
        self._label = ready_label_id
        self._repository = target_repository
        self._registry = registry
        self._transport = transport or UrllibTrelloTransport()

    def list_items(self) -> Sequence[WorkItem]:
        payload = self._get(f"lists/{self._list}/cards", {"fields": "id"})
        if not isinstance(payload, list):
            raise TrelloBacklogError("invalid Trello card list")
        items: list[WorkItem] = []
        for card in payload:
            if not isinstance(card, Mapping) or not _card_id(card.get("id")):
                raise TrelloBacklogError("invalid Trello card identity")
            items.append(self.get_item(str(card["id"])))
        return items

    def get_item(self, external_id: str) -> WorkItem:
        if not _card_id(external_id):
            raise TrelloBacklogError("invalid Trello card identity")
        card = self._get(
            f"cards/{external_id}",
            {"fields": "id,idList,closed,name,desc,idLabels"},
        )
        checklists = self._get(
            f"cards/{external_id}/checklists", {"fields": "name", "checkItems": "all"}
        )
        if not isinstance(card, Mapping) or card.get("id") != external_id:
            raise TrelloBacklogError("invalid Trello card payload")
        title = card.get("name")
        body = card.get("desc")
        labels = card.get("idLabels")
        if (
            not isinstance(title, str)
            or not title.strip()
            or not isinstance(body, str)
            or not isinstance(labels, list)
            or not all(isinstance(label, str) for label in labels)
            or not isinstance(card.get("closed"), bool)
            or not isinstance(card.get("idList"), str)
        ):
            raise TrelloBacklogError("invalid Trello card fields")
        body = source_description(body)
        project_id = "ai-factory-lab"
        repository = self._repository
        if self._registry is not None:
            declarations = re.findall(r"(?im)^project_id\s*[:=]\s*([^\s]+)\s*$", body)
            forged = re.search(r"(?im)^target_repository\s*[:=]", body)
            project_id = declarations[0] if len(declarations) == 1 else "invalid"
            try:
                repository = self._registry.resolve(project_id).repository_slug
            except ProjectRoutingError:
                repository = "invalid/invalid"
            if forged:
                repository = "invalid/invalid"
        return WorkItem(
            provider="trello",
            external_id=external_id,
            title=title,
            body=body,
            target_repository=repository,
            project_id=project_id,
            eligible=(
                card["idList"] == self._list and not card["closed"] and self._label in labels
            ),
            dependencies_satisfied=_dependencies_satisfied(checklists),
        )

    def _get(self, path: str, params: Mapping[str, str]) -> Any:  # noqa: ANN401
        url = "https://api.trello.com/1/" + path + "?" + urllib.parse.urlencode(params)
        headers = {
            "Authorization": f'OAuth oauth_consumer_key="{self._key}", oauth_token="{self._token}"',
            "Accept": "application/json",
        }
        failed = False
        result: Any = None
        try:
            result = self._transport.get_json(url, headers)
        except Exception:  # noqa: BLE001 - discard provider text and credentials
            failed = True
        if failed:
            raise TrelloBacklogError("Trello read failed")
        return result


def _card_id(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9]+", value) is not None


def _dependencies_satisfied(payload: Any) -> bool:  # noqa: ANN401
    if not isinstance(payload, list):
        raise TrelloBacklogError("invalid Trello dependencies")
    for checklist in payload:
        if not isinstance(checklist, Mapping) or not isinstance(checklist.get("name"), str):
            raise TrelloBacklogError("invalid Trello dependencies")
        if checklist["name"].casefold() != "dependencies":
            continue
        items = checklist.get("checkItems")
        if not isinstance(items, list):
            raise TrelloBacklogError("invalid Trello dependencies")
        for item in items:
            if not isinstance(item, Mapping) or item.get("state") not in {"complete", "incomplete"}:
                raise TrelloBacklogError("invalid Trello dependency state")
            if item["state"] != "complete":
                return False
    return True
