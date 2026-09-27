"""Command-line entry point for AI Factory Lab.

Two commands exist:

* ``--show-config`` / ``--version`` — Phase 1 configuration sanity check.
* ``intake`` — one read-only issue intake pass.
* ``run`` — one bounded, resumable task through the existing Phases 2A–5.

There is deliberately no daemon, scheduler or polling loop: intake runs once,
when a human asks for it.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence

from factory import __version__
from factory.domain.enums import RepositoryRole
from factory.domain.models import Repository
from factory.infrastructure.config import FactoryConfig, UnsupportedDatabaseError
from factory.infrastructure.logging import configure_logging
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
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
from factory.integrations.workspace import (
    GitWorkspacePublisher,
    GitWorkspaceRevisionInspector,
    GitWorktreeWorkspaceProvisioner,
)
from factory.orchestration.intake import IssueIntakeService
from factory.orchestration.runtime import FactoryRuntime, RuntimeResult

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
        help="Run at most one factory-ready task through WAITING_HUMAN.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.show_config:
        config = FactoryConfig.from_env()
        print(json.dumps(config.redacted(), indent=2, sort_keys=True))
        return EXIT_OK

    if args.command == "intake":
        return _run_intake(FactoryConfig.from_env())
    if args.command == "run":
        return _run_runtime(FactoryConfig.from_env())

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


def _run_runtime(config: FactoryConfig) -> int:
    """Build the production graph and execute exactly one resumable task."""
    try:
        repository = _resolve_repository(config)
        if config.github.target_repo is None:
            raise ConfigurationError("FACTORY_TARGET_REPO is required for run")
        if config.source_checkout is None:
            raise ConfigurationError("FACTORY_SOURCE_CHECKOUT is required for run")
        if config.openhands.base_url is None:
            raise ConfigurationError("OPENHANDS_BASE_URL is required for run")
        if config.openhands.agent_profile_id is None:
            raise ConfigurationError("OPENHANDS_AGENT_PROFILE_ID is required for run")
        if config.github_write_token is None:
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
        mapper = WorkspacePathMapper(config.workspace_root, config.openhands_workspace_root)
        adapter = OpenHandsAdapter(
            OpenHandsClient(
                config.openhands.base_url,
                session_api_key=config.openhands.session_api_key,
            ),
            OpenHandsExecution(agent_profile_id=config.openhands.agent_profile_id),
            workspace_paths=mapper,
        )
        write_client = GitHubWriteClient(config.github_write_token, config.github.api_url)
        runtime = FactoryRuntime(
            intake=intake,
            intake_repository=repository,
            tasks=tasks,
            runs=runs,
            adapter=adapter,
            provisioner=GitWorktreeWorkspaceProvisioner(
                config.source_checkout, base_ref=config.workspace_base_ref
            ),
            workspace_root=config.workspace_root,
            gate_specs=config.quality_gates,
            gate_runner=LocalQualityGateRunner(),
            revision_inspector=GitWorkspaceRevisionInspector(),
            publisher=GitWorkspacePublisher(
                write_token=config.github_write_token,
                allowed_git_host=config.github_git_host,
            ),
            pull_request_sink=GitHubPullRequestSink(write_client),
            pull_requests=pull_requests,
            base_branch=config.target_default_branch,
            poll_interval=config.run_poll_interval,
            timeout=config.run_timeout,
        )
        result = runtime.run_once()
    except (ConfigurationError, UnsupportedDatabaseError) as exc:
        print(f"configuration error: {exc}")
        return EXIT_CONFIG_ERROR
    except Exception as exc:  # noqa: BLE001 - runtime boundary is secret-safe by design
        print(f"run failed: {type(exc).__name__}")
        return EXIT_INTAKE_ERROR

    _print_runtime_result(result)
    return EXIT_OK if result.outcome in {"WAITING_HUMAN", "NO_ELIGIBLE_TASK"} else EXIT_INTAKE_ERROR


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
