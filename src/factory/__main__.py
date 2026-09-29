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
from types import FrameType
from typing import Any
from uuid import UUID

from factory import __version__
from factory.domain.enums import RepositoryRole
from factory.domain.errors import RetryNotAllowedError, TaskStateChangedError
from factory.domain.models import AgentAdapter, Repository
from factory.infrastructure.config import AgentEngine, FactoryConfig, UnsupportedDatabaseError
from factory.infrastructure.logging import configure_logging
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.integrations.codex import CodexAdapter
from factory.integrations.gates import LocalQualityGateRunner
from factory.integrations.github import (
    GitHubClient,
    GitHubIssueSource,
    GitHubPullRequestSink,
    GitHubWriteClient,
)
from factory.integrations.openhands import (
    OpenHandsAdapter,
    OpenHandsClient,
    OpenHandsExecution,
    WorkspacePathMapper,
)
from factory.integrations.operational import ScratchAcceptance, ScratchWorkspaceProvisioner
from factory.integrations.workspace import (
    GitWorkspacePublisher,
    GitWorkspaceRevisionInspector,
    GitWorktreeWorkspaceProvisioner,
)
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.retry import RetryService
from factory.orchestration.runtime import FactoryRuntime, RuntimeResult
from factory.orchestration.watch import FactoryWatcher, WatchOutcome

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
        help="Automatic worker: sequential one-task iterations (WIP=1) with idle waits.",
    )
    retry = subparsers.add_parser(
        "retry", help="Make one BLOCKED task or failed legacy CLAIMED task READY."
    )
    retry.add_argument("--task-id", required=True, type=UUID, help="UUID of the task to recover.")
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
    if args.command == "retry":
        return _run_retry(config, str(args.task_id))
    if args.command == "run":
        return _run_runtime(config)
    if args.command == "watch":
        return _run_watch(config)

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
    source = GitHubIssueSource(
        client,
        target_repository=config.github.target_repo,
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


def _run_retry(config: FactoryConfig, task_id: str) -> int:
    """Recover only the requested task using local repositories; never run intake."""
    try:
        database = config.database
        tasks = SqliteTaskRepository(database.path)
        runs = SqliteRunRepository(database.path)
        tasks.initialize()
        runs.initialize()
        task = RetryService(tasks, runs).retry(task_id)
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
        if result.outcome in {
            "WAITING_HUMAN", "OPERATIONAL_DONE", "NO_ELIGIBLE_TASK", "SOURCE_INELIGIBLE"
        }
        else EXIT_INTAKE_ERROR
    )


def _run_watch(config: FactoryConfig) -> int:
    """Drive the automatic worker until a signal or human gate, WIP=1."""
    try:
        runtime = _build_runtime(config)
    except (ConfigurationError, UnsupportedDatabaseError) as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - build boundary is secret-safe by design
        print(f"watch failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR

    stop = threading.Event()
    previous_handlers = _install_stop_handlers(stop)
    try:
        watcher = FactoryWatcher(
            runtime=runtime,
            idle_interval=config.watch_idle_interval,
            should_stop=stop.is_set,
        )
        outcome = watcher.run()
    except Exception as exc:  # noqa: BLE001 - runtime boundary is secret-safe by design
        print(f"watch failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)

    _print_watch_outcome(outcome)
    return EXIT_OK


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


def _build_runtime(config: FactoryConfig) -> FactoryRuntime:
    """Validate the run prerequisites and build the production runtime graph."""
    repository = _resolve_repository(config)
    if config.github.target_repo is None and config.operational_scratch_root is None:
        raise ConfigurationError("FACTORY_TARGET_REPO is required for run")
    if config.source_checkout is None and config.operational_scratch_root is None:
        raise ConfigurationError("FACTORY_SOURCE_CHECKOUT is required for run")
    if config.github_write_token is None and config.operational_scratch_root is None:
        raise ConfigurationError("GITHUB_WRITE_TOKEN is required before publication")

    database = config.database
    tasks = SqliteTaskRepository(database.path)
    runs = SqliteRunRepository(database.path)
    pull_requests = SqlitePullRequestRepository(database.path)
    tasks.initialize()
    runs.initialize()
    pull_requests.initialize()

    read_client = GitHubClient(token=config.github.token or "", api_url=config.github.api_url)
    intake = IssueIntakeService(
        source=GitHubIssueSource(read_client, target_repository=config.github.target_repo),
        repository=tasks,
    )
    adapter = _build_agent_adapter(config)
    write_client = GitHubWriteClient(config.github_write_token or "", config.github.api_url)
    operational_provisioner = (
        ScratchWorkspaceProvisioner(config.operational_scratch_root)
        if config.operational_scratch_root is not None
        else None
    )
    return FactoryRuntime(
        intake=intake,
        intake_repository=repository,
        tasks=tasks,
        runs=runs,
        adapter=adapter,
        provisioner=GitWorktreeWorkspaceProvisioner(
            config.source_checkout or "", base_ref=config.workspace_base_ref
        ),
        workspace_root=config.workspace_root,
        gate_specs=config.quality_gates,
        gate_runner=LocalQualityGateRunner(),
        revision_inspector=GitWorkspaceRevisionInspector(),
        publisher=GitWorkspacePublisher(
            write_token=config.github_write_token,
            write_username=config.github_write_username,
            allowed_git_host=config.github_git_host,
        ),
        pull_request_sink=GitHubPullRequestSink(write_client),
        pull_requests=pull_requests,
        base_branch=config.target_default_branch,
        operational_provisioner=operational_provisioner,
        operational_root=config.operational_scratch_root,
        operational_acceptance=(
            ScratchAcceptance(operational_provisioner)
            if operational_provisioner is not None else None
        ),
        code_capable=(
            config.github.target_repo is not None
            and config.source_checkout is not None
            and config.github_write_token is not None
        ),
        poll_interval=config.run_poll_interval,
        timeout=config.run_timeout,
    )


def _build_agent_adapter(config: FactoryConfig) -> AgentAdapter:
    """Select exactly the configured execution engine."""
    if config.agent_engine is AgentEngine.CODEX:
        return CodexAdapter(timeout=config.run_timeout)
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
