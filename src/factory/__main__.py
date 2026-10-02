"""Command-line entry point for AI Factory Lab.

Available commands:

* ``--show-config`` / ``--version`` — Phase 1 configuration sanity check.
* ``intake`` — one read-only issue intake pass.
* ``retry --task-id`` — explicitly recover one task without dispatch.
* ``run`` — one bounded, resumable code or scratch operational task.
* ``watch`` — the automatic worker: sequential ``run`` iterations, WIP=1.

Neither ``run`` nor ``watch`` merges, deploys or mutates an Issue. Both stop at
``WAITING_HUMAN`` for code tasks and ``DONE`` for accepted scratch tasks.
``watch`` is the only mode that loops; it reuses
``FactoryRuntime.run_once`` rather than re-implementing any phase.
"""

from __future__ import annotations

import argparse
import json
import signal
import threading
from collections.abc import Sequence
from pathlib import Path
from types import FrameType
from typing import Any
from uuid import UUID

from factory import __version__
from factory.domain.enums import RepositoryRole, TaskStatus
from factory.domain.errors import RetryNotAllowedError, TaskStateChangedError
from factory.domain.models import AgentAdapter, Repository
from factory.domain.operational import OperationalCapability, OperationalPolicy
from factory.domain.ports import IssueSource
from factory.infrastructure.config import AgentEngine, FactoryConfig, UnsupportedDatabaseError
from factory.infrastructure.logging import configure_logging
from factory.infrastructure.persistence import (
    SqliteBacklogLinkRepository,
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.feedback_sqlite import SqliteFeedbackEventRepository
from factory.infrastructure.persistence.metrics import SqliteSprintMetrics
from factory.infrastructure.persistence.security import SqliteSecurityReviewGate
from factory.infrastructure.persistence.sprint_sqlite import SqliteSprintRepository
from factory.infrastructure.persistence.status_events import SqliteStatusEventStore
from factory.integrations.codex import CodexAdapter
from factory.integrations.context.repository import RepositoryContextSource
from factory.integrations.context.skill_source import ApprovedSkillSource
from factory.integrations.database_readonly import SqliteReadonlyInspector
from factory.integrations.gates import LocalQualityGateRunner
from factory.integrations.github import (
    GitHubClient,
    GitHubIssueSource,
    GitHubPullRequestSink,
    GitHubWriteClient,
)
from factory.integrations.github.backlog_issues import GitHubBacklogIssueSink
from factory.integrations.github.delivery import GitHubDeliveryEvidenceSource
from factory.integrations.github.issue_completion import GitHubIssueCompletionSink
from factory.integrations.github.pr_state import GitHubPullRequestStateSource
from factory.integrations.github.project_issues import ProjectIssueSource
from factory.integrations.openhands import (
    OpenHandsAdapter,
    OpenHandsClient,
    OpenHandsExecution,
    WorkspacePathMapper,
)
from factory.integrations.operational import ScratchAcceptance, ScratchWorkspaceProvisioner
from factory.integrations.project_routing import (
    ProjectContextSource,
    ProjectSkillSource,
    ProjectWorkspaceProvisioner,
)
from factory.integrations.security.review import GitSecurityInspector
from factory.integrations.status_http import serve_status
from factory.integrations.trello.backlog import TrelloBacklogSource
from factory.integrations.trello.feedback import TrelloWorkItemFeedbackSink
from factory.integrations.trello.status import TrelloStatusChannel
from factory.integrations.workspace import (
    GitWorkspacePublisher,
    GitWorkspaceRevisionInspector,
    GitWorktreeWorkspaceProvisioner,
    git_workspace_is_clean_unpublished,
)
from factory.orchestration.backlog import BacklogMaterializationService
from factory.orchestration.context import ContextPackBuilder
from factory.orchestration.feedback import FeedbackReconciliationService
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.recovery import RecoveryPolicy
from factory.orchestration.retry import RetryService
from factory.orchestration.rework import ReworkNotAllowedError, ReworkService
from factory.orchestration.runtime import FactoryRuntime, RuntimeResult
from factory.orchestration.sprint import AuthorizedBacklogSource, SprintService
from factory.orchestration.status import StatusService
from factory.orchestration.status_events import StatusEventPublisher
from factory.orchestration.terminal_recovery import (
    TerminalRecoveryRefused,
    TerminalRecoveryService,
)
from factory.orchestration.transitions import TaskLifecycleService
from factory.orchestration.watch import WatchOutcome
from factory.orchestration.worker_pool import WorkerPool, WorkerSession

EXIT_OK = 0
EXIT_CONFIG_ERROR = 2
EXIT_INTAKE_ERROR = 1


class ConfigurationError(Exception):
    """Required runtime configuration is missing or invalid."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description="AI Factory Lab control plane.")
    parser.add_argument("--version", action="version", version=f"ai-factory-lab {__version__}")
    parser.add_argument(
        "--show-config",
        action="store_true",
        help="Print the resolved configuration with all credentials masked.",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser(
        "intake",
        help="Discover eligible GitHub Issues and persist them as factory tasks.",
    )
    subparsers.add_parser(
        "run",
        help="Run at most one factory-ready task through its acceptance state.",
    )
    subparsers.add_parser(
        "watch",
        help="Compatibility alias for the continuous concurrent worker pool.",
    )
    subparsers.add_parser("pool", help="Run up to two isolated task workers concurrently.")
    status = subparsers.add_parser("status", help="Read the current factory status.")
    status.add_argument("--serve", action="store_true", help="Serve a read-only phone view.")
    status.add_argument("--host", default="127.0.0.1")
    status.add_argument("--port", type=int, default=8765)
    subparsers.add_parser("sync-status", help="Deliver queued status events to Trello.")
    subparsers.add_parser(
        "metrics", help="Read deterministic global and per-project sprint metrics."
    )
    backlog = subparsers.add_parser(
        "sync-backlog", help="Reconcile Trello Sprint cards into GitHub Issues."
    )
    backlog.add_argument("--card-id", help="Reconcile one card from a verified webhook event.")
    sprint = subparsers.add_parser("sprint", help="Plan, authorize or control a bounded sprint.")
    sprint.add_argument(
        "action",
        choices=("plan", "authorize", "status", "pause", "resume", "cancel", "request-human"),
        help="Choose a sprint operation; pause and request-human require --sprint-id.",
    )
    sprint.add_argument("--manifest", help="JSON file with sprint_id and ordered items.")
    sprint.add_argument("--sprint-id", help="Authorized sprint id for a state decision.")
    retry = subparsers.add_parser(
        "retry", help="Recover one blocked task: resume publication or authorize a fresh attempt."
    )
    retry.add_argument("--task-id", required=True, type=UUID, help="UUID of the task to recover.")
    override = subparsers.add_parser(
        "security-override", help="Record a human decision for one blocked security review."
    )
    override.add_argument("--task-id", required=True, type=UUID)
    override.add_argument("--run-id", required=True, type=UUID)
    override.add_argument("--actor", required=True)
    override.add_argument("--reason-file", required=True)
    timeout_recovery = subparsers.add_parser(
        "recover-timeout",
        help="Operator-evidenced recovery of one FAILED Codex timeout; never dispatches.",
    )
    timeout_recovery.add_argument("--task-id", required=True, type=UUID)
    timeout_recovery.add_argument("--run-id", required=True, type=UUID)
    timeout_recovery.add_argument(
        "--acknowledge-timeout",
        action="store_true",
        help="Explicitly attest to a reviewed worker timeout and request recovery.",
    )
    worker_recovery = subparsers.add_parser(
        "recover-worker-failure",
        help="Recover one clean unpublished FAILED Codex worker exit; never dispatches.",
    )
    worker_recovery.add_argument("--task-id", required=True, type=UUID)
    worker_recovery.add_argument("--run-id", required=True, type=UUID)
    worker_recovery.add_argument(
        "--acknowledge-worker-failure",
        action="store_true",
        help="Explicitly attest that the reviewed worker failure may be recovered.",
    )
    orphan_recovery = subparsers.add_parser(
        "recover-orphaned-codex",
        help="Recover one orphaned RUNNING Codex attempt; never dispatches.",
    )
    orphan_recovery.add_argument("--task-id", required=True, type=UUID)
    orphan_recovery.add_argument("--run-id", required=True, type=UUID)
    orphan_recovery.add_argument("--acknowledge-orphan", action="store_true")
    rework = subparsers.add_parser(
        "request-changes", help="Record human QA feedback for an open PR."
    )
    rework.add_argument("--task-id", required=True, type=UUID)
    rework.add_argument(
        "--feedback-file", required=True, help="UTF-8 file containing reviewed QA feedback."
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.show_config and args.command is None:
        parser.print_help()
        return EXIT_OK
    try:
        config = FactoryConfig.from_env()
    except ValueError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR

    if args.show_config:
        print(json.dumps(config.redacted(), indent=2, sort_keys=True))
        return EXIT_OK

    if args.command == "intake":
        return _run_intake(config)
    if args.command == "sync-backlog":
        return _run_backlog(config, args.card_id)
    if args.command == "sprint":
        return _run_sprint(config, args.action, args.manifest, args.sprint_id)
    if args.command == "retry":
        return _run_retry(config, str(args.task_id))
    if args.command == "security-override":
        return _security_override(
            config, str(args.task_id), str(args.run_id), args.actor, args.reason_file
        )
    if args.command == "recover-timeout":
        return _recover_terminal_timeout(
            config, str(args.task_id), str(args.run_id), args.acknowledge_timeout
        )
    if args.command == "recover-worker-failure":
        return _recover_worker_failure(
            config,
            str(args.task_id),
            str(args.run_id),
            args.acknowledge_worker_failure,
        )
    if args.command == "recover-orphaned-codex":
        return _recover_orphaned_codex(
            config, str(args.task_id), str(args.run_id), args.acknowledge_orphan
        )
    if args.command == "request-changes":
        return _request_changes(config, str(args.task_id), args.feedback_file)
    if args.command == "run":
        return _run_runtime(config)
    if args.command == "watch":
        return _run_pool(config)
    if args.command == "pool":
        return _run_pool(config)
    if args.command == "status":
        return _show_status(config, serve=args.serve, host=args.host, port=args.port)
    if args.command == "sync-status":
        try:
            if config.project_registry is not None:
                runtime = _build_runtime(config)
                runtime.prepare_pool()
            publisher = _status_publisher(config)
            if publisher is not None:
                publisher.flush()
            elif config.project_registry is None:
                print("configuration error: provider reconciliation requires project registry")
                return EXIT_CONFIG_ERROR
        except Exception as exc:  # noqa: BLE001 - provider errors may contain credentials
            print(f"status sync failed: {type(exc).__name__}")
            return EXIT_INTAKE_ERROR
        return EXIT_OK
    if args.command == "metrics":
        try:
            metrics = SqliteSprintMetrics(config.database.path)
            metrics.initialize()
            print(json.dumps([row.as_dict() for row in metrics.report()], sort_keys=True))
            return EXIT_OK
        except Exception as exc:  # noqa: BLE001 - persistence details stay private
            print(f"metrics failed: {type(exc).__name__}")
            return EXIT_INTAKE_ERROR

    parser.print_help()
    return EXIT_OK


def _run_intake(config: FactoryConfig) -> int:
    """Execute one intake pass. Returns a process exit code."""
    try:
        repository = _resolve_repository(config)
    except ConfigurationError as exc:
        # The message never contains a credential.
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR

    configure_logging(config.logging)

    try:
        database = config.database
        task_repository = SqliteTaskRepository(database.path)
    except UnsupportedDatabaseError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR

    task_repository.initialize()

    assert config.github.token is not None  # guaranteed by _resolve_repository
    client = GitHubClient(token=config.github.token, api_url=config.github.api_url)
    raw_source = GitHubIssueSource(
        client,
        target_repository=None if config.project_registry else config.github.target_repo,
    )
    source: IssueSource = (
        ProjectIssueSource(raw_source, config.project_registry)
        if config.project_registry
        else raw_source
    )
    service = IssueIntakeService(source=source, repository=task_repository)

    try:
        summary = service.intake(repository)
    except Exception as exc:  # noqa: BLE001 - surface a clean, secret-free failure
        # Never print a raw exception message: an upstream client could embed a
        # credential in it. Only the type is shown, and the configured token is
        # additionally redacted as defense in depth.
        detail = _redact(str(exc), config.github.token)
        print(f"intake failed: {type(exc).__name__}: {detail}")
        return EXIT_INTAKE_ERROR

    print(f"Discovered: {summary.discovered}")
    print(f"Created: {summary.created}")
    print(f"Existing: {summary.existing}")
    print(f"Errors: {summary.errors}")
    return EXIT_OK if summary.errors == 0 else EXIT_INTAKE_ERROR


def _build_backlog(config: FactoryConfig) -> BacklogMaterializationService | None:
    """Build optional backlog reconciliation without changing GitHub intake."""
    list_id = config.trello_backlog_list_id
    label_id = config.trello_ready_label_id
    if list_id is None and label_id is None:
        return None
    if not all((list_id, label_id, config.trello_key, config.trello_token)):
        raise ConfigurationError("Trello backlog list, READY label, key and token are required")
    if config.github.control_plane_repo is None or config.github_write_token is None:
        raise ConfigurationError("GitHub repository and write token are required for backlog")
    assert list_id is not None and label_id is not None
    assert config.trello_key is not None and config.trello_token is not None
    links = SqliteBacklogLinkRepository(config.database.path)
    links.initialize()
    return BacklogMaterializationService(
        TrelloBacklogSource(
            config.trello_key,
            config.trello_token,
            list_id,
            label_id,
            config.github.control_plane_repo,
            registry=config.project_registry,
        ),
        GitHubBacklogIssueSink(GitHubWriteClient(config.github_write_token, config.github.api_url)),
        links,
        registry=config.project_registry,
    )


def _build_sprint(config: FactoryConfig, tasks: SqliteTaskRepository) -> SprintService | None:
    """Use E2's exact ports with an immutable, authorized source guard."""
    if config.trello_backlog_list_id is None and config.trello_ready_label_id is None:
        return None
    if not all(
        (
            config.trello_backlog_list_id,
            config.trello_ready_label_id,
            config.trello_key,
            config.trello_token,
        )
    ):
        raise ConfigurationError("Trello backlog list, READY label, key and token are required")
    if config.github.control_plane_repo is None or config.github_write_token is None:
        raise ConfigurationError("GitHub repository and write token are required for backlog")
    assert config.trello_key is not None and config.trello_token is not None
    assert config.trello_backlog_list_id is not None and config.trello_ready_label_id is not None
    source = TrelloBacklogSource(
        config.trello_key,
        config.trello_token,
        config.trello_backlog_list_id,
        config.trello_ready_label_id,
        config.github.control_plane_repo,
        registry=config.project_registry,
    )
    sprints = SqliteSprintRepository(config.database.path)
    links = SqliteBacklogLinkRepository(config.database.path)
    wrapped = AuthorizedBacklogSource(source, sprints)
    materializer = BacklogMaterializationService(
        wrapped,
        GitHubBacklogIssueSink(GitHubWriteClient(config.github_write_token, config.github.api_url)),
        links,
        registry=config.project_registry,
    )
    return SprintService(
        source,
        materializer,
        links,
        tasks,
        sprints,
        registry=config.project_registry,
        feedback_events=(
            SqliteFeedbackEventRepository(config.database.path)
            if config.project_registry is not None
            else None
        ),
    )


def _run_backlog(config: FactoryConfig, card_id: str | None) -> int:
    try:
        service = _build_backlog(config)
        if service is None:
            raise ConfigurationError("Trello backlog adapter is not enabled")
        summary = service.reconcile(card_id)
    except (ConfigurationError, UnsupportedDatabaseError) as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - provider failures may contain secrets
        print(f"backlog sync failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    print(f"Examined: {summary.examined}")
    print(f"Created: {summary.created}")
    print(f"Existing: {summary.existing}")
    print(f"Ineligible: {summary.ineligible}")
    print(f"Uncertain: {summary.uncertain}")
    return EXIT_OK if summary.uncertain == 0 else EXIT_INTAKE_ERROR


def _run_sprint(
    config: FactoryConfig, action: str, manifest_file: str | None, sprint_id: str | None
) -> int:
    """Require an explicit operator command for every authorization or resume."""
    try:
        tasks = SqliteTaskRepository(config.database.path)
        sprints = SqliteSprintRepository(config.database.path)
        if (
            action == "plan"
            and config.database.path != ":memory:"
            and not Path(config.database.path).exists()
        ):
            raise ConfigurationError("database must exist for side-effect-free planning")
        if action != "plan":
            tasks.initialize()
            sprints.initialize()
        service = _build_sprint(config, tasks)
        if service is None:
            raise ConfigurationError("Trello backlog adapter is not enabled")
        if action in {"plan", "authorize"}:
            if manifest_file is None:
                raise ValueError("--manifest is required")
            raw = json.loads(Path(manifest_file).read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("items"), list):
                raise ValueError("invalid sprint manifest")
            entries = tuple(
                (str(item["external_id"]), tuple(str(dep) for dep in item.get("dependencies", [])))
                for item in raw["items"]
            )
            manifest = service.draft(str(raw["sprint_id"]), entries)
            plan = service.plan(manifest)
            print(
                json.dumps(
                    {
                        "sprint_id": manifest.sprint_id,
                        "wip_limit": 1,
                        "steps": [
                            {
                                "position": row.position,
                                "external_id": row.external_id,
                                "eligible": row.eligible,
                                "blockers": row.blockers,
                            }
                            for row in plan
                        ],
                    },
                    indent=2,
                )
            )
            if action == "authorize":
                service.authorize(manifest)
                print("Sprint authorized")
        elif action == "status":
            current = sprints.current()
            print(
                json.dumps(
                    {
                        "sprint_id": current[0].sprint_id,
                        "state": current[1].value,
                        "position": current[2],
                    }
                    if current is not None
                    else {"state": "UNAUTHORIZED"}
                )
            )
        elif action in {"pause", "request-human"}:
            if sprint_id is None:
                raise ValueError("--sprint-id is required")
            service.pause(sprint_id)
            print("Sprint paused for human decision")
        elif action == "resume":
            if sprint_id is None:
                raise ValueError("--sprint-id is required")
            service.resume(sprint_id)
            print("Sprint resumed")
        elif action == "cancel":
            if sprint_id is None:
                raise ValueError("--sprint-id is required")
            service.cancel(sprint_id)
            print("Sprint cancelled")
    except (ConfigurationError, UnsupportedDatabaseError, ValueError, KeyError, TypeError) as exc:
        print(f"sprint refused: {type(exc).__name__}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - provider text and credentials are not public
        print(f"sprint failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    return EXIT_OK


def _show_status(config: FactoryConfig, *, serve: bool, host: str, port: int) -> int:
    """Read only durable state; serving is loopback by default."""
    if host not in {"127.0.0.1", "localhost", "::1"}:
        print("configuration error: status server must bind to loopback")
        return EXIT_CONFIG_ERROR
    try:
        path = config.database.path
        tasks = SqliteTaskRepository(path)
        runs = SqliteRunRepository(path)
        prs = SqlitePullRequestRepository(path)
        tasks.initialize()
        runs.initialize()
        prs.initialize()
        service = StatusService(
            tasks,
            runs,
            prs,
            heartbeat_interval=config.heartbeat_interval,
            missed_heartbeats=config.missed_heartbeats,
        )
        if serve:
            publisher = _status_publisher(config)
            serve_status(
                service,
                host,
                port,
                publisher.flush if publisher else None,
                SqliteAuditEventStore(path),
            )
        else:
            from dataclasses import asdict

            print(json.dumps(asdict(service.current()), indent=2))
    except Exception as exc:  # noqa: BLE001 - storage and path details are not public
        print(f"status failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    return EXIT_OK


def _status_publisher(config: FactoryConfig) -> StatusEventPublisher | None:
    card_id = config.trello_status_card_id
    key = config.trello_key
    token = config.trello_token
    if card_id is None or key is None or token is None:
        return None
    path = config.database.path
    tasks = SqliteTaskRepository(path)
    runs = SqliteRunRepository(path)
    prs = SqlitePullRequestRepository(path)
    events = SqliteStatusEventStore(path)
    tasks.initialize()
    runs.initialize()
    prs.initialize()
    events.initialize()
    service = StatusService(
        tasks,
        runs,
        prs,
        heartbeat_interval=config.heartbeat_interval,
        missed_heartbeats=config.missed_heartbeats,
    )
    channel = TrelloStatusChannel(card_id, key, token)
    return StatusEventPublisher(service, events, channel, channel)


def _recover_terminal_timeout(
    config: FactoryConfig, task_id: str, run_id: str, acknowledged: bool
) -> int:
    """Exceptional operator action: fail closed, never dispatch or resume."""
    if not acknowledged:
        print("recover-timeout refused: --acknowledge-timeout is required")
        return EXIT_INTAKE_ERROR
    try:
        token = config.github.token
        write_token = config.github_write_token
        source_checkout = config.source_checkout
        if not token or not write_token or (not source_checkout and not config.project_registry):
            raise ConfigurationError(
                "GitHub read/write credentials and source checkout are required"
            )
        tasks = SqliteTaskRepository(config.database.path)
        runs = SqliteRunRepository(config.database.path)
        prs = SqlitePullRequestRepository(config.database.path)
        tasks.initialize()
        runs.initialize()
        prs.initialize()
        raw_source = GitHubIssueSource(
            GitHubClient(token=token, api_url=config.github.api_url),
            target_repository=None if config.project_registry else config.github.target_repo,
        )
        source: IssueSource = (
            ProjectIssueSource(raw_source, config.project_registry)
            if config.project_registry
            else raw_source
        )
        intake = IssueIntakeService(source=source, repository=tasks)
        service = TerminalRecoveryService(
            tasks,
            runs,
            prs,
            GitHubPullRequestSink(GitHubWriteClient(write_token, config.github.api_url)),
            (
                ProjectWorkspaceProvisioner(config.project_registry)
                if config.project_registry
                else GitWorktreeWorkspaceProvisioner(
                    source_checkout or "", base_ref=config.workspace_base_ref
                )
            ),
            workspace_root=config.workspace_root,
            base_branch=config.target_default_branch,
            registry=config.project_registry,
            source_is_eligible=lambda task: (
                task.source is not None
                and intake.is_eligible(
                    Repository(task.source.repository_slug, role=RepositoryRole.CONTROL_PLANE),
                    task.source,
                )
            ),
            sprint=_build_sprint(config, tasks),
        )
        result = service.authorize(task_id, run_id, acknowledge_timeout=True)
    except (ConfigurationError, UnsupportedDatabaseError) as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except KeyError:
        print("recover-timeout refused: task was not found")
        return EXIT_INTAKE_ERROR
    except (TerminalRecoveryRefused, TaskStateChangedError) as exc:
        print(f"recover-timeout refused: {exc}")
        return EXIT_INTAKE_ERROR
    except Exception as exc:  # noqa: BLE001 - do not expose remote/provider values
        print(f"recover-timeout failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    print(f"Task: {result.task_id}")
    print("Task status: BLOCKED")
    print("Next steps: explicit factory retry, then human Sprint resume.")
    return EXIT_OK


def _recover_orphaned_codex(
    config: FactoryConfig, task_id: str, run_id: str, acknowledged: bool
) -> int:
    if not acknowledged:
        print("recover-orphaned-codex refused: --acknowledge-orphan is required")
        return EXIT_INTAKE_ERROR
    try:
        token, write_token = config.github.token, config.github_write_token
        source_checkout = config.source_checkout
        if not token or not write_token or (not source_checkout and not config.project_registry):
            raise ConfigurationError(
                "GitHub read/write credentials and source checkout are required"
            )
        tasks, runs, prs = (
            SqliteTaskRepository(config.database.path),
            SqliteRunRepository(config.database.path),
            SqlitePullRequestRepository(config.database.path),
        )
        tasks.initialize()
        runs.initialize()
        prs.initialize()
        raw_source = GitHubIssueSource(
            GitHubClient(token=token, api_url=config.github.api_url),
            target_repository=None if config.project_registry else config.github.target_repo,
        )
        source: IssueSource = (
            ProjectIssueSource(raw_source, config.project_registry)
            if config.project_registry
            else raw_source
        )
        intake = IssueIntakeService(source=source, repository=tasks)
        service = TerminalRecoveryService(
            tasks,
            runs,
            prs,
            GitHubPullRequestSink(GitHubWriteClient(write_token, config.github.api_url)),
            ProjectWorkspaceProvisioner(config.project_registry)
            if config.project_registry
            else GitWorktreeWorkspaceProvisioner(
                source_checkout or "", base_ref=config.workspace_base_ref
            ),
            workspace_root=config.workspace_root,
            base_branch=config.target_default_branch,
            registry=config.project_registry,
            source_is_eligible=lambda task: (
                task.source is not None
                and intake.is_eligible(
                    Repository(task.source.repository_slug, role=RepositoryRole.CONTROL_PLANE),
                    task.source,
                )
            ),
            workspace_is_clean=git_workspace_is_clean_unpublished,
            sprint=_build_sprint(config, tasks),
        )
        result = service.authorize_orphaned_codex(task_id, run_id, acknowledge_orphan=True)
    except (ConfigurationError, UnsupportedDatabaseError) as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except KeyError:
        print("recover-orphaned-codex refused: task was not found")
        return EXIT_INTAKE_ERROR
    except (TerminalRecoveryRefused, TaskStateChangedError, ValueError) as exc:
        print(f"recover-orphaned-codex refused: {exc}")
        return EXIT_INTAKE_ERROR
    except Exception as exc:  # noqa: BLE001 - keep provider details out of CLI
        print(f"recover-orphaned-codex failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    print(f"Task: {result.task_id}\nTask status: BLOCKED\nNext step: explicit factory retry.")
    return EXIT_OK


def _recover_worker_failure(
    config: FactoryConfig, task_id: str, run_id: str, acknowledged: bool
) -> int:
    """Recover one clean, unpublished non-timeout Codex worker failure."""
    if not acknowledged:
        print("recover-worker-failure refused: --acknowledge-worker-failure is required")
        return EXIT_INTAKE_ERROR
    try:
        token = config.github.token
        write_token = config.github_write_token
        source_checkout = config.source_checkout
        if not token or not write_token or (not source_checkout and not config.project_registry):
            raise ConfigurationError(
                "GitHub read/write credentials and source checkout are required"
            )
        tasks = SqliteTaskRepository(config.database.path)
        runs = SqliteRunRepository(config.database.path)
        prs = SqlitePullRequestRepository(config.database.path)
        tasks.initialize()
        runs.initialize()
        prs.initialize()
        raw_source = GitHubIssueSource(
            GitHubClient(token=token, api_url=config.github.api_url),
            target_repository=None if config.project_registry else config.github.target_repo,
        )
        source: IssueSource = (
            ProjectIssueSource(raw_source, config.project_registry)
            if config.project_registry
            else raw_source
        )
        intake = IssueIntakeService(source=source, repository=tasks)
        service = TerminalRecoveryService(
            tasks,
            runs,
            prs,
            GitHubPullRequestSink(GitHubWriteClient(write_token, config.github.api_url)),
            (
                ProjectWorkspaceProvisioner(config.project_registry)
                if config.project_registry
                else GitWorktreeWorkspaceProvisioner(
                    source_checkout or "", base_ref=config.workspace_base_ref
                )
            ),
            workspace_root=config.workspace_root,
            base_branch=config.target_default_branch,
            registry=config.project_registry,
            source_is_eligible=lambda task: (
                task.source is not None
                and intake.is_eligible(
                    Repository(task.source.repository_slug, role=RepositoryRole.CONTROL_PLANE),
                    task.source,
                )
            ),
            workspace_is_clean=git_workspace_is_clean_unpublished,
            sprint=_build_sprint(config, tasks),
        )
        result = service.authorize_worker_failure(task_id, run_id, acknowledge_failure=True)
    except (ConfigurationError, UnsupportedDatabaseError) as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except KeyError:
        print("recover-worker-failure refused: task was not found")
        return EXIT_INTAKE_ERROR
    except (TerminalRecoveryRefused, TaskStateChangedError) as exc:
        print(f"recover-worker-failure refused: {exc}")
        return EXIT_INTAKE_ERROR
    except Exception as exc:  # noqa: BLE001 - do not expose remote/provider values
        print(f"recover-worker-failure failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    print(f"Task: {result.task_id}")
    print("Task status: BLOCKED")
    print("Next steps: explicit factory retry.")
    return EXIT_OK


def _security_override(
    config: FactoryConfig, task_id: str, run_id: str, actor: str, reason_file: str
) -> int:
    """Resume only the exact blocked revision after a recorded human decision."""
    try:
        tasks = SqliteTaskRepository(config.database.path)
        runs = SqliteRunRepository(config.database.path)
        tasks.initialize()
        runs.initialize()
        task = tasks.get(task_id)
        run = runs.get_run(run_id)
        if (
            task is None
            or run is None
            or run.task_id != task_id
            or task.status is not TaskStatus.BLOCKED
            or task.blocked_reason != "security review blocked publication"
            or not runs.list_runs(task_id)
            or runs.list_runs(task_id)[-1].run_id != run_id
        ):
            raise ValueError("task is not blocked on this security review")
        path = Path(reason_file)
        if path.stat().st_size > 4096:
            raise ValueError("decision reason too large")
        reason = path.read_text(encoding="utf-8")
        gate = SqliteSecurityReviewGate(
            config.database.path,
            GitSecurityInspector(
                base_ref=config.workspace_base_ref or config.target_default_branch,
                registry=config.project_registry,
            ),
        )
        review = gate.review(task, run)
        gate.record_override(task, run, review, actor=actor, reason=reason)
        TaskLifecycleService(tasks).transition(task_id, TaskStatus.VALIDATING)
    except Exception as exc:  # noqa: BLE001 - all source and path details are private
        print(f"security override refused: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    print("Security override recorded; task returned to VALIDATING")
    return EXIT_OK


def _run_retry(config: FactoryConfig, task_id: str) -> int:
    """Recover only the requested task using local repositories; never run intake."""
    try:
        database = config.database
        tasks = SqliteTaskRepository(database.path)
        runs = SqliteRunRepository(database.path)
        tasks.initialize()
        runs.initialize()
        task = RetryService(tasks, runs, RecoveryPolicy()).retry(task_id)
    except UnsupportedDatabaseError as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except KeyError:
        print(f"retry refused: task {task_id} not found")
        return EXIT_INTAKE_ERROR
    except (RetryNotAllowedError, TaskStateChangedError) as exc:
        print(f"retry refused: {exc}")
        return EXIT_INTAKE_ERROR
    except Exception as exc:  # noqa: BLE001 - never print storage errors or paths
        print(f"retry failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    print(f"Task: {task.task_id}")
    print(f"Task status: {task.status.value}")
    return EXIT_OK


def _request_changes(config: FactoryConfig, task_id: str, feedback_file: str) -> int:
    """Record a human rejection after confirming the exact PR is still open."""
    try:
        feedback_path = Path(feedback_file)
        if feedback_path.stat().st_size > 16000:
            raise ReworkNotAllowedError("QA feedback file is too large")
        feedback = feedback_path.read_text(encoding="utf-8")
        if config.github.token is None:
            raise ConfigurationError("GITHUB_TOKEN is required to verify the PR")
        tasks = SqliteTaskRepository(config.database.path)
        runs = SqliteRunRepository(config.database.path)
        prs = SqlitePullRequestRepository(config.database.path)
        tasks.initialize()
        runs.initialize()
        prs.initialize()
        state = GitHubPullRequestStateSource(
            GitHubClient(token=config.github.token, api_url=config.github.api_url)
        )
        task = ReworkService(tasks, runs, prs, state).request(task_id, feedback)
    except (ConfigurationError, UnsupportedDatabaseError) as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except (ReworkNotAllowedError, TaskStateChangedError, KeyError) as exc:
        print(f"request-changes refused: {exc}")
        return EXIT_INTAKE_ERROR
    except Exception as exc:  # noqa: BLE001 - no raw provider, path or credential text
        print(f"request-changes failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    print(f"Task: {task.task_id}")
    print(f"Task status: {task.status.value}")
    return EXIT_OK


def _run_runtime(config: FactoryConfig) -> int:
    """Build the production graph and execute exactly one resumable task."""
    try:
        runtime = _build_runtime(config)
    except (ConfigurationError, UnsupportedDatabaseError) as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - runtime boundary is secret-safe by design
        print(f"run failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR

    try:
        result = runtime.run_once()
    except Exception as exc:  # noqa: BLE001 - runtime boundary is secret-safe by design
        print(f"run failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR

    _print_runtime_result(result)
    return (
        EXIT_OK
        if result.outcome
        in {
            "WAITING_HUMAN",
            "OPERATIONAL_DONE",
            "NO_ELIGIBLE_TASK",
            "SOURCE_INELIGIBLE",
            "SPRINT_PAUSED",
        }
        else EXIT_INTAKE_ERROR
    )


def _run_watch(config: FactoryConfig) -> int:
    """Drive the automatic worker until a signal, using the concurrent pool."""
    return _run_pool(config)


def _run_pool(config: FactoryConfig) -> int:
    """Run independent task sessions while preserving durable human gates."""
    try:
        _build_runtime(config, pool_mode=True)
    except (ConfigurationError, UnsupportedDatabaseError) as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - no provider details in output
        print(f"pool failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    stop = threading.Event()
    previous_handlers = _install_stop_handlers(stop)
    try:
        WorkerPool(
            lambda: _build_runtime(config, pool_mode=True),
            max_concurrency=config.max_concurrency,
            idle_interval=config.watch_idle_interval,
            should_stop=stop.is_set,
            on_session=_print_worker_session,
        ).run()
    except Exception as exc:  # noqa: BLE001 - no provider details in output
        print(f"pool failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
    return EXIT_OK


def _print_worker_session(session: WorkerSession) -> None:
    """Expose only worker identity and sanitized lifecycle facts."""
    if session.result is not None:
        _print_runtime_result(session.result)
    else:
        print(f"Worker {session.task_id} failed: {session.error_type}")


def _install_stop_handlers(stop: threading.Event) -> dict[int, Any]:
    """Install SIGINT/SIGTERM handlers that request a cooperative stop.

    Returns the previous handlers so the caller can restore them. A signal only
    sets the event; the watch loop observes it between iterations, so a stop
    request never interrupts an in-flight task or corrupts persisted state.
    """
    installed: dict[int, Any] = {}

    def _request_stop(signum: int, frame: FrameType | None) -> None:
        del signum, frame
        stop.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        installed[signum] = signal.signal(signum, _request_stop)
    return installed


def _build_runtime(config: FactoryConfig, *, pool_mode: bool = False) -> FactoryRuntime:
    """Validate the run prerequisites and build the production runtime graph."""
    repository = _resolve_repository(config)
    if (
        config.github.target_repo is None
        and config.project_registry is None
        and config.operational_scratch_root is None
        and not config.database_readonly_targets
    ):
        raise ConfigurationError("FACTORY_TARGET_REPO is required for run")
    if (
        config.source_checkout is None
        and config.project_registry is None
        and config.operational_scratch_root is None
        and not config.database_readonly_targets
    ):
        raise ConfigurationError("FACTORY_SOURCE_CHECKOUT is required for run")
    if (
        config.github_write_token is None
        and config.operational_scratch_root is None
        and not config.database_readonly_targets
    ):
        raise ConfigurationError("GITHUB_WRITE_TOKEN is required before publication")

    database = config.database
    tasks = SqliteTaskRepository(
        database.path, max_active_claims=config.max_concurrency if pool_mode else 1
    )
    runs = SqliteRunRepository(database.path)
    pull_requests = SqlitePullRequestRepository(database.path)
    tasks.initialize()
    runs.initialize()
    pull_requests.initialize()
    status_publisher = _status_publisher(config)
    sprint = _build_sprint(config, tasks)
    backlog = _build_backlog(config)

    read_client = GitHubClient(token=config.github.token or "", api_url=config.github.api_url)
    issue_source = GitHubIssueSource(
        read_client,
        target_repository=(None if config.project_registry else config.github.target_repo),
    )
    intake = IssueIntakeService(
        source=(
            ProjectIssueSource(issue_source, config.project_registry)
            if config.project_registry
            else issue_source
        ),
        repository=tasks,
    )
    adapter = _build_agent_adapter(config)
    write_client = GitHubWriteClient(config.github_write_token or "", config.github.api_url)
    feedback = None
    if config.project_registry is not None:
        card_feedback = (
            TrelloWorkItemFeedbackSink(
                config.trello_key,
                config.trello_token,
                config.project_registry,
                done_list_id=config.trello_done_list_id,
            )
            if config.trello_key is not None and config.trello_token is not None
            else None
        )
        feedback = FeedbackReconciliationService(
            tasks,
            runs,
            pull_requests,
            GitHubDeliveryEvidenceSource(read_client, config.project_registry),
            GitHubIssueCompletionSink(write_client),
            card_feedback,
            SqliteFeedbackEventRepository(database.path),
            SqliteBacklogLinkRepository(database.path),
            config.project_registry,
            SqliteSprintRepository(database.path) if sprint is not None else None,
        )
    operational_provisioner = (
        ScratchWorkspaceProvisioner(config.operational_scratch_root)
        if config.operational_scratch_root is not None
        else None
    )
    database_targets = dict(config.database_readonly_targets)
    operational_policy = OperationalPolicy(
        enabled=frozenset(
            ({OperationalCapability.SCRATCH} if config.operational_scratch_root else set())
            | ({OperationalCapability.DATABASE_READONLY} if database_targets else set())
        ),
        hosts=frozenset({"local"}) if database_targets else frozenset(),
        paths=frozenset(database_targets.values()),
        commands=frozenset({"inspect"}) if database_targets else frozenset(),
        targets=frozenset(database_targets),
    )
    return FactoryRuntime(
        intake=intake,
        intake_repository=repository,
        tasks=tasks,
        runs=runs,
        adapter=adapter,
        context_builder=ContextPackBuilder(
            (
                (
                    ProjectContextSource(config.project_registry)
                    if config.project_registry
                    else RepositoryContextSource(config.source_checkout or "")
                ),
                (
                    ProjectSkillSource(config.project_registry, config.codex_ecc_skill)
                    if config.project_registry
                    else ApprovedSkillSource(config.codex_ecc_skill)
                ),
            )
        ),
        provisioner=(
            ProjectWorkspaceProvisioner(config.project_registry)
            if config.project_registry
            else GitWorktreeWorkspaceProvisioner(
                config.source_checkout or "", base_ref=config.workspace_base_ref
            )
        ),
        workspace_root=config.workspace_root,
        gate_specs=config.quality_gates,
        registry=config.project_registry,
        feedback=feedback,
        pool_mode=pool_mode,
        pool_backlog_links=(SqliteBacklogLinkRepository(database.path) if pool_mode else None),
        task_gate_specs=config.task_quality_gates,
        gate_runner=LocalQualityGateRunner(),
        revision_inspector=GitWorkspaceRevisionInspector(),
        security_review=SqliteSecurityReviewGate(
            config.database.path,
            GitSecurityInspector(
                base_ref=config.workspace_base_ref or config.target_default_branch,
                registry=config.project_registry,
            ),
        ),
        publisher=GitWorkspacePublisher(
            write_token=config.github_write_token,
            write_username=config.github_write_username,
            allowed_git_host=config.github_git_host,
        ),
        pull_request_sink=GitHubPullRequestSink(write_client),
        pull_requests=pull_requests,
        pull_request_state=GitHubPullRequestStateSource(read_client),
        base_branch=config.target_default_branch,
        operational_provisioner=operational_provisioner,
        operational_policy=operational_policy,
        database_inspector=(
            SqliteReadonlyInspector(database_targets) if database_targets else None
        ),
        operational_root=config.operational_scratch_root,
        operational_acceptance=(
            ScratchAcceptance(operational_provisioner)
            if operational_provisioner is not None
            else None
        ),
        code_capable=(
            (
                config.project_registry is not None
                or (config.github.target_repo is not None and config.source_checkout is not None)
            )
            and config.github_write_token is not None
        ),
        poll_interval=config.run_poll_interval,
        timeout=config.run_timeout,
        heartbeat_interval=config.heartbeat_interval,
        status_pulse=status_publisher.flush if status_publisher else None,
        backlog_reconcile=backlog.reconcile if backlog is not None else None,
        sprint=sprint,
        recovery_policy=RecoveryPolicy(),
    )


def _build_agent_adapter(config: FactoryConfig) -> AgentAdapter:
    """Select exactly the configured execution engine."""
    if config.agent_engine is AgentEngine.CODEX:
        return CodexAdapter(
            timeout=config.run_timeout,
            ecc_skill=config.codex_ecc_skill,
            model=config.codex_model,
            reasoning_effort=config.codex_reasoning_effort,
        )
    if config.agent_engine is AgentEngine.OPENHANDS:
        if config.openhands.base_url is None:
            raise ConfigurationError("OPENHANDS_BASE_URL is required for run")
        if config.openhands.agent_profile_id is None:
            raise ConfigurationError("OPENHANDS_AGENT_PROFILE_ID is required for run")
        mapper = WorkspacePathMapper(config.workspace_root, config.openhands_workspace_root)
        return OpenHandsAdapter(
            OpenHandsClient(
                config.openhands.base_url,
                session_api_key=config.openhands.session_api_key,
            ),
            OpenHandsExecution(
                agent_profile_id=config.openhands.agent_profile_id,
                shared_workspace_hook_command=config.openhands_shared_workspace_hook_command,
            ),
            workspace_paths=mapper,
        )
    raise ConfigurationError("unsupported agent engine")


def _print_watch_outcome(outcome: WatchOutcome) -> None:
    """Print only counters and the sanitized last result, never provider text."""
    print(f"Iterations: {outcome.iterations}")
    print(f"Processed: {outcome.processed}")
    print(f"Idle waits: {outcome.idle_waits}")
    print(f"Stopped: {'yes' if outcome.stopped else 'no'}")
    if outcome.last_result is not None:
        _print_runtime_result(outcome.last_result)


def _print_runtime_result(result: RuntimeResult) -> None:
    """Print only stable identifiers and sanitized state, never provider text."""
    print(f"Task: {result.task_id or '-'}")
    print(f"Run: {result.run_id or '-'}")
    print(f"Task status: {result.task_status.value if result.task_status else '-'}")
    print(f"Branch: {result.branch or '-'}")
    print(f"Validation: {result.validation.value if result.validation else '-'}")
    pull_request = f"{result.pull_request_number or '-'} {result.pull_request_url or ''}".rstrip()
    print(f"Pull request: {pull_request}")
    print(f"Outcome: {result.outcome}")


def _redact(message: str, secret: str | None) -> str:
    """Replace any occurrence of ``secret`` in ``message`` with a mask.

    Belt-and-braces protection for the one place the CLI prints an exception
    string: even a third-party error must never echo the configured token.
    """
    if not secret:
        return message
    return message.replace(secret, "***")


def _resolve_repository(config: FactoryConfig) -> Repository:
    """Validate the runtime configuration intake needs and build the repo handle."""
    if config.github.token is None:
        raise ConfigurationError("GITHUB_TOKEN is required for intake")
    slug = config.github.control_plane_repo
    if not slug:
        raise ConfigurationError("FACTORY_GITHUB_REPO is required for intake")
    if "/" not in slug:
        raise ConfigurationError("FACTORY_GITHUB_REPO must be 'owner/name'")
    return Repository(slug=slug, role=RepositoryRole.CONTROL_PLANE)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
