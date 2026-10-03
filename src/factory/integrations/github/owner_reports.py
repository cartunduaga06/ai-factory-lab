"""Sanitized, replace-in-place execution reports on source GitHub Issues."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from factory.domain.enums import RunStatus, TaskStatus, ValidationOutcome
from factory.domain.models import AgentRun, FactoryTask, PullRequest
from factory.infrastructure.persistence.audit import AuditEvent
from factory.integrations.github.write_client import GitHubWriteClient, GitHubWriteError


class OwnerReportError(RuntimeError):
    """Sanitized source Issue report failure."""


def _safe_text(value: str | None, limit: int = 300) -> str:
    if not value:
        return "-"
    # Free-form agent/provider/task text is untrusted and can contain secrets,
    # personal data, commands, or terminal control characters. Keep reports
    # limited to ordinary short text and never include raw command output.
    text = re.sub(r"(?i)gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+", "[redacted]", value)
    text = re.sub(
        r"(?i)(token|password|secret|api[_-]?key)\s*[:=]\s*\S+",
        "[redacted]",
        text,
    )
    text = re.sub(r"(?i)\b(password|secret|token|stdout|stderr|command)\b", "[redacted]", text)
    text = re.sub(r"[^A-Za-z0-9 .,;:_/@+#()\-\[\]=]", " ", text)
    text = " ".join(text.split())[:limit]
    return text or "-"


def _marker(task_id: str) -> str:
    return f"<!-- factory-owner-report:{hashlib.sha256(task_id.encode()).hexdigest()} -->"


def _safe_revision(value: str | None) -> str:
    return value if value and re.fullmatch(r"[0-9a-fA-F]{7,64}", value) else "-"


class GitHubOwnerReportSink:
    """Update a single exact source Issue report using only allowlisted facts."""

    def __init__(self, client: GitHubWriteClient) -> None:
        self._client = client

    def publish(
        self,
        task: FactoryTask,
        runs: tuple[AgentRun, ...],
        pull_requests: tuple[PullRequest, ...],
        events: tuple[AuditEvent, ...],
        *,
        initial: bool = False,
    ) -> None:
        source = task.source
        if source is None or source.provider != "github":
            return
        path = f"/repos/{source.repository_slug}/issues/{source.issue_number}"
        marker = _marker(task.task_id)
        initial = (
            initial
            and not runs
            and not any(
                event.name not in {"IssueMaterialized", "WorkItemReady"} for event in events
            )
        )
        body = self._render(task, runs, pull_requests, events, marker, initial=initial)
        try:
            issue = self._client.get(path)
            self._check_issue(issue, source.repository_slug, source.issue_number)
            comments = self._find_reports(path, marker)
            if comments:
                primary = comments[0]
                if primary.get("body") != body:
                    self._client.patch(f"{path}/comments/{primary['id']}", {"body": body})
                for duplicate in comments[1:]:
                    self._client.delete(f"{path}/comments/{duplicate['id']}")
            else:
                self._client.post(path + "/comments", {"body": body})
        except OwnerReportError:
            raise
        except GitHubWriteError:
            raise OwnerReportError("GitHub owner report update failed") from None
        except Exception:
            raise OwnerReportError("GitHub owner report update failed") from None

    def _find_reports(self, path: str, marker: str) -> list[Mapping[str, Any]]:
        found: list[Mapping[str, Any]] = []
        for page in range(1, 101):
            payload = self._client.get(path + "/comments", {"per_page": 100, "page": page})
            if not isinstance(payload, list):
                raise OwnerReportError("invalid Issue comments")
            found.extend(
                item
                for item in payload
                if isinstance(item, Mapping)
                and item.get("user") is not None
                and isinstance(item.get("body"), str)
                and marker in item["body"].splitlines()
                and isinstance(item.get("id"), int)
            )
            if len(payload) < 100:
                return found
        raise OwnerReportError("Issue comment lookup exceeded page limit")

    @staticmethod
    def _check_issue(issue: object, repository: str, number: int) -> None:
        if not isinstance(issue, Mapping):
            raise OwnerReportError("invalid Issue identity")
        url = issue.get("html_url")
        parsed = urlparse(url) if isinstance(url, str) else None
        if (
            issue.get("number") != number
            or "pull_request" in issue
            or parsed is None
            or parsed.scheme != "https"
            or parsed.hostname != "github.com"
            or parsed.path != f"/{repository}/issues/{number}"
            or parsed.query
            or parsed.fragment
        ):
            raise OwnerReportError("Issue identity mismatch")

    @staticmethod
    def _render(
        task: FactoryTask,
        runs: tuple[AgentRun, ...],
        pull_requests: tuple[PullRequest, ...],
        events: tuple[AuditEvent, ...],
        marker: str,
        *,
        initial: bool,
    ) -> str:
        latest = runs[-1] if runs else None
        pr = next(
            (
                item
                for item in reversed(pull_requests)
                if item.run_id == (latest.run_id if latest else None)
            ),
            None,
        )
        validation = latest.validation_outcome if latest else ValidationOutcome.PENDING
        if task.status is TaskStatus.WAITING_HUMAN or (pr is not None and not pr.merged):
            final = "WAITING_HUMAN"
        elif task.status in {TaskStatus.BLOCKED, TaskStatus.FAILED, TaskStatus.CANCELLED} or (
            latest is not None and latest.status in {RunStatus.FAILED, RunStatus.CANCELLED}
        ):
            final = "BLOCKED" if task.status is TaskStatus.BLOCKED else "FAILED"
        elif (
            not initial
            and task.status is TaskStatus.DONE
            and pr is not None
            and pr.merged
            and any(event.name == "DeliveryReconciled" for event in events)
        ):
            final = "PASS"
        else:
            final = (
                "BLOCKED"
                if task.blocked_reason
                else "FAILED"
                if (
                    (latest and latest.status is RunStatus.FAILED)
                    or (latest and latest.validation_outcome is ValidationOutcome.GATES_FAILED)
                )
                else "BLOCKED"
            )
        execution = latest.status.value if latest is not None else "NOT_STARTED"
        acceptance = (
            "WAITING_HUMAN"
            if final == "WAITING_HUMAN"
            else ("PASS" if final == "PASS" else "NOT_ACCEPTED")
        )
        event_names = ", ".join(_safe_text(event.name, 64) for event in events[-12:]) or "-"
        gates = (
            "\n".join(
                f"- {_safe_text(gate.name, 64)}: {gate.status.value} "
                f"({'required' if gate.required else 'optional'})"
                for gate in latest.gates
            )
            if latest and latest.gates
            else "-"
        )
        branch = _safe_text(latest.workspace.branch if latest and latest.workspace else None, 160)
        commit = _safe_revision(pr.commit_sha if pr else None)
        pr_url = urlparse(pr.url) if pr and pr.url else None
        valid_pr_url = bool(
            pr is not None
            and pr.number
            and pr_url is not None
            and pr_url.scheme == "https"
            and pr_url.hostname == "github.com"
            and pr_url.path == f"/{pr.repository_slug}/pull/{pr.number}"
            and not pr_url.query
            and not pr_url.fragment
        )
        pr_ref = (
            f"[#{pr.number}](https://github.com/{pr.repository_slug}/pull/{pr.number})"
            if valid_pr_url and pr
            else "-"
        )
        issue_link = (
            f"https://github.com/{task.source.repository_slug}/issues/{task.source.issue_number}"
            if task.source
            else "-"
        )
        trace_items = [
            f"E1 `{_safe_text(event.name, 64)}` #{event.sequence}" for event in events[-12:]
        ]
        trace = f"[{issue_link}] ({', '.join(trace_items)})" if events else "-"
        action = (
            f"Review and merge PR #{pr.number} when satisfied; otherwise request changes."
            if pr and pr.number
            else "Review this report and resolve the listed blocker before authorizing another run."
            if final in {"FAILED", "BLOCKED"}
            else "No action yet; execution is in progress."
        )
        run_id = (
            latest.run_id if latest and re.fullmatch(r"[0-9a-fA-F-]{36}", latest.run_id) else "-"
        )
        lines = [
            "## Factory execution report",
            "",
            f"Objective: {_safe_text(task.title)}",
            f"What executed: {_safe_text(execution)}; task status {task.status.value}.",
            "",
            "### Outcomes",
            f"- Execution: {execution}",
            f"- Validation: {validation.value}",
            f"- Acceptance: {acceptance}",
            "",
            "### Changes and evidence",
            "- Code / DB / service / infra changes: "
            f"{_safe_text(latest.summary if latest else None)}",
            f"- Tests / gates (no command output):\n{gates}",
            f"- E1 trace events: {event_names}",
            "- What was not done: merge, deployment, and unverified changes.",
            f"- Risks / blockers: {_safe_text(task.blocked_reason)}",
            "",
            "### Trace",
            f"- task_id: `{_safe_text(task.task_id, 80)}`",
            f"- run_id: `{run_id}`",
            f"- branch: `{branch}`",
            f"- commit: `{commit}`",
            f"- PR: {pr_ref}",
            f"- E1 links: {trace}",
            f"- Owner Action: {action}",
            f"- Final status: **{final}**",
            "",
            marker,
        ]
        return "\n".join(lines)


__all__ = ["GitHubOwnerReportSink", "OwnerReportError"]
