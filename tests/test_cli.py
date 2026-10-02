"""Tests for the manual ``factory intake`` command.

The CLI is exercised through :func:`factory.__main__.main` with an explicit
environment mapping and an injected fake issue source, so nothing touches the
network or the real environment. The focus is exit codes and that no secret ever
reaches stdout.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import factory.__main__ as cli
from factory.domain.enums import AgentKind, RunStatus, TaskStatus
from factory.domain.models import AgentRun, FactoryTask, Repository, TaskSource
from factory.domain.ports import IssueSource
from factory.infrastructure.config import FactoryConfig
from factory.infrastructure.persistence import SqliteRunRepository, SqliteTaskRepository
from factory.integrations.codex import CodexAdapter
from factory.integrations.openhands import OpenHandsAdapter
from factory.orchestration.intake import IntakeSummary
from factory.orchestration.runtime import RuntimeResult
from factory.orchestration.watch import WatchOutcome

TOKEN = "ghp_cli_secret_token_never_print"


class FakeIssueSource(IssueSource):
    def __init__(self, tasks: list[FactoryTask]) -> None:
        self._tasks = tasks

    def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
        return list(self._tasks)

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        for task in self._tasks:
            if task.source == source:
                return task
        raise KeyError(source)

    def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
        return any(task.source == source for task in self._tasks)


class ExplodingIssueSource(IssueSource):
    def list_open_tasks(self, repository: Repository) -> list[FactoryTask]:
        raise RuntimeError(f"boom while using token {TOKEN}")

    def get_task(self, repository: Repository, source: TaskSource) -> FactoryTask:
        raise KeyError(source)

    def is_eligible(self, repository: Repository, source: TaskSource) -> bool:
        raise RuntimeError(f"boom while using token {TOKEN}")


def _issue(number: int) -> FactoryTask:
    return FactoryTask(
        title=f"Issue {number}",
        target_repository="cartunduaga06/finanza-ia",
        source=TaskSource("github", "cartunduaga06/ai-factory-lab", number),
        labels=("factory-ready",),
    )


@pytest.fixture
def patched_intake(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[dict[str, object]]:
    """Patch the CLI's source factory so intake never reaches GitHub."""
    state: dict[str, object] = {}

    def install(source: IssueSource) -> None:
        state["source"] = source

        def fake_source_factory(*args: object, **kwargs: object) -> IssueSource:
            return source

        monkeypatch.setattr(cli, "GitHubIssueSource", fake_source_factory)

    state["install"] = install
    yield state


RELEVANT_KEYS = (
    "GITHUB_TOKEN",
    "GITHUB_API_URL",
    "FACTORY_GITHUB_REPO",
    "FACTORY_TARGET_REPO",
    "DATABASE_URL",
    "FACTORY_ENV",
    "FACTORY_LOG_LEVEL",
    "FACTORY_LOG_FORMAT",
)


def _env(tmp_path: Path, **overrides: str) -> dict[str, str]:
    env = {
        "GITHUB_TOKEN": TOKEN,
        "FACTORY_GITHUB_REPO": "cartunduaga06/ai-factory-lab",
        "FACTORY_TARGET_REPO": "cartunduaga06/finanza-ia",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'factory.db'}",
    }
    env.update(overrides)
    return env


def _run(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str]:
    # Drive the real FactoryConfig.from_env through os.environ so the CLI path
    # is exercised exactly as in production. Ambient variables are cleared first
    # so the test never reads a real credential.
    for key in RELEVANT_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)

    code = cli.main(["intake"])
    return code, capsys.readouterr().out


def test_successful_intake_prints_summary(
    patched_intake: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patched_intake["install"](FakeIssueSource([_issue(10), _issue(11)]))  # type: ignore[operator]
    code, out = _run(monkeypatch, _env(tmp_path), capsys)

    assert code == 0
    assert "Discovered: 2" in out
    assert "Created: 2" in out
    assert "Existing: 0" in out
    assert "Errors: 0" in out


def test_second_run_reports_existing(
    patched_intake: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patched_intake["install"](FakeIssueSource([_issue(10), _issue(11)]))  # type: ignore[operator]
    _run(monkeypatch, _env(tmp_path), capsys)
    code, out = _run(monkeypatch, _env(tmp_path), capsys)

    assert code == 0
    assert "Created: 0" in out
    assert "Existing: 2" in out


def test_missing_github_token_fails_with_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _env(tmp_path)
    del env["GITHUB_TOKEN"]
    code, out = _run(monkeypatch, env, capsys)

    assert code == cli.EXIT_CONFIG_ERROR
    assert "configuration error" in out


def test_missing_github_repo_fails_with_config_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = _env(tmp_path)
    del env["FACTORY_GITHUB_REPO"]
    code, out = _run(monkeypatch, env, capsys)

    assert code == cli.EXIT_CONFIG_ERROR
    assert "configuration error" in out


def test_unsupported_database_scheme_fails_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code, out = _run(monkeypatch, _env(tmp_path, DATABASE_URL="postgresql://localhost/x"), capsys)

    assert code == cli.EXIT_CONFIG_ERROR
    assert "configuration error" in out


def test_intake_failure_returns_nonzero_without_leaking_secrets(
    patched_intake: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patched_intake["install"](ExplodingIssueSource())  # type: ignore[operator]
    code, out = _run(monkeypatch, _env(tmp_path), capsys)

    assert code == cli.EXIT_INTAKE_ERROR
    assert "intake failed" in out
    # The token appears in the exception the fake raises; the CLI must not print it.
    assert TOKEN not in out


def test_success_never_prints_the_token(
    patched_intake: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    patched_intake["install"](FakeIssueSource([_issue(10)]))  # type: ignore[operator]
    _, out = _run(monkeypatch, _env(tmp_path), capsys)
    assert TOKEN not in out


def test_summary_dataclass_defaults_are_zero() -> None:
    summary = IntakeSummary()
    assert (summary.discovered, summary.created, summary.existing, summary.errors) == (0, 0, 0, 0)


def _runtime_env(
    tmp_path: Path,
    *,
    write_token: str | None = "write-secret",
    **overrides: str,
) -> dict[str, str]:
    values = {
        "GITHUB_TOKEN": TOKEN,
        "FACTORY_GITHUB_REPO": "cartunduaga06/ai-factory-lab",
        "FACTORY_TARGET_REPO": "cartunduaga06/finanza-ia",
        "FACTORY_SOURCE_CHECKOUT": str(tmp_path),
        "OPENHANDS_BASE_URL": "http://openhands.invalid",
        "OPENHANDS_AGENT_PROFILE_ID": "profile-1",
        "FACTORY_WORKSPACE_ROOT": str(tmp_path / "host-workspaces"),
        "OPENHANDS_WORKSPACE_ROOT": "/projects",
        "DATABASE_URL": f"sqlite:///{tmp_path / 'factory.db'}",
    }
    if write_token is not None:
        values["GITHUB_WRITE_TOKEN"] = write_token
    values.update(overrides)
    return values


def test_default_runtime_selects_codex_without_openhands_configuration(tmp_path: Path) -> None:
    config = FactoryConfig.from_env(_runtime_env(tmp_path))
    assert isinstance(cli._build_agent_adapter(config), CodexAdapter)


def test_runtime_passes_configured_write_username_to_publisher(tmp_path: Path) -> None:
    config = FactoryConfig.from_env(
        _runtime_env(tmp_path, GITHUB_WRITE_USERNAME="publication-user")
    )
    runtime = cli._build_runtime(config)
    assert runtime._publication._publisher._write_username == "publication-user"


def test_runtime_reconciles_configured_trello_backlog(tmp_path: Path) -> None:
    config = FactoryConfig.from_env(
        _runtime_env(
            tmp_path,
            FACTORY_TRELLO_BACKLOG_LIST_ID="backlog123",
            FACTORY_TRELLO_READY_LABEL_ID="ready123",
            FACTORY_TRELLO_KEY="trello-key",
            FACTORY_TRELLO_TOKEN="trello-token",
        )
    )

    runtime = cli._build_runtime(config)

    assert runtime._backlog_reconcile is not None


def test_legacy_watch_command_uses_pool_supervisor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[bool] = []

    def run_pool(config: FactoryConfig) -> int:
        seen.append(config.max_concurrency == 2)
        return cli.EXIT_OK

    monkeypatch.setattr(cli, "_run_pool", run_pool)
    assert cli.main(["watch"]) == cli.EXIT_OK
    assert seen == [True]


def test_explicit_openhands_selection_preserves_local_adapter(tmp_path: Path) -> None:
    config = FactoryConfig.from_env(_runtime_env(tmp_path, FACTORY_AGENT_ENGINE="openhands"))
    assert isinstance(cli._build_agent_adapter(config), OpenHandsAdapter)


def test_explicit_openhands_never_falls_back_to_codex(tmp_path: Path) -> None:
    env = _runtime_env(tmp_path, FACTORY_AGENT_ENGINE="openhands")
    del env["OPENHANDS_BASE_URL"]
    with pytest.raises(cli.ConfigurationError, match="OPENHANDS_BASE_URL"):
        cli._build_agent_adapter(FactoryConfig.from_env(env))


def test_invalid_engine_exits_with_config_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("FACTORY_AGENT_ENGINE", "unknown")
    assert cli.main(["run"]) == cli.EXIT_CONFIG_ERROR
    assert "FACTORY_AGENT_ENGINE" in capsys.readouterr().out


def test_run_without_write_token_fails_closed_before_provider_access(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GITHUB_WRITE_TOKEN", raising=False)
    for key, value in _runtime_env(tmp_path, write_token=None).items():
        monkeypatch.setenv(key, value)

    code = cli.main(["run"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_CONFIG_ERROR
    assert "GITHUB_WRITE_TOKEN is required" in captured.out
    assert TOKEN not in captured.out
    assert TOKEN not in captured.err


def test_runtime_cli_does_not_echo_provider_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_secret = "provider-secret-must-not-escape"

    class ExplodingRuntime:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def run_once(self) -> object:
            raise RuntimeError(provider_secret)

    monkeypatch.setattr(cli, "FactoryRuntime", ExplodingRuntime)
    for key, value in _runtime_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    code = cli.main(["run"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_INTAKE_ERROR
    assert provider_secret not in captured.out
    assert provider_secret not in captured.err


@pytest.mark.parametrize("status", [TaskStatus.BLOCKED, TaskStatus.READY, TaskStatus.CLAIMED])
def test_retry_cli_changes_only_requested_blocked_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    status: TaskStatus,
) -> None:
    database = str(tmp_path / "retry.db")
    config = FactoryConfig.from_env({"DATABASE_URL": f"sqlite:///{database}"})
    monkeypatch.setattr(cli.FactoryConfig, "from_env", lambda: config)
    tasks = SqliteTaskRepository(database)
    tasks.initialize()
    task = tasks.save(
        FactoryTask(title="requested", target_repository="example/target", status=status)
    )
    other = tasks.save(FactoryTask(title="other", target_repository="example/target"))
    code = cli.main(["retry", "--task-id", task.task_id])
    output = capsys.readouterr().out
    if status is TaskStatus.BLOCKED:
        assert code == cli.EXIT_OK
        assert "Task status: READY" in output
        assert tasks.get(task.task_id).status is TaskStatus.READY
        assert len(tasks.history(task.task_id)) == 1
    else:
        assert code == cli.EXIT_INTAKE_ERROR
        message = "latest run FAILED" if status is TaskStatus.CLAIMED else "not BLOCKED"
        assert message in output
        assert tasks.get(task.task_id).status is status
        assert tasks.history(task.task_id) == []
    assert tasks.get(other.task_id) == other
    assert SqliteRunRepository(database).list_runs() == []


def test_retry_cli_unknown_task_fails_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config = FactoryConfig.from_env({"DATABASE_URL": f"sqlite:///{tmp_path / 'retry.db'}"})
    monkeypatch.setattr(cli.FactoryConfig, "from_env", lambda: config)
    assert cli.main(["retry", "--task-id", "00000000-0000-0000-0000-000000000000"]) == 1
    assert "not found" in capsys.readouterr().out


@pytest.mark.parametrize("arguments", [["retry"], ["retry", "--task-id", "invalid"]])
def test_retry_cli_requires_uuid(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        cli.main(arguments)
    assert caught.value.code == 2


def test_retry_cli_recovers_legacy_claim_without_processing_other_tasks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = str(tmp_path / "retry.db")
    config = FactoryConfig.from_env({"DATABASE_URL": f"sqlite:///{database}"})
    monkeypatch.setattr(cli.FactoryConfig, "from_env", lambda: config)
    tasks = SqliteTaskRepository(database)
    runs = SqliteRunRepository(database)
    tasks.initialize()
    runs.initialize()
    task = tasks.save(
        FactoryTask(title="legacy", target_repository="example/target", status=TaskStatus.CLAIMED)
    )
    other = tasks.save(FactoryTask(title="other", target_repository="example/target"))
    failed = runs.save_run(
        AgentRun(task_id=task.task_id, adapter=AgentKind.OTHER, status=RunStatus.FAILED)
    )
    assert cli.main(["retry", "--task-id", task.task_id]) == cli.EXIT_OK
    assert "Task status: READY" in capsys.readouterr().out
    assert tasks.get(task.task_id).status is TaskStatus.READY
    assert [(edge.from_status, edge.to_status) for edge in tasks.history(task.task_id)] == [
        (TaskStatus.CLAIMED, TaskStatus.BLOCKED),
        (TaskStatus.BLOCKED, TaskStatus.READY),
    ]
    assert tasks.get(other.task_id) == other
    assert runs.list_runs() == [failed]


# -- automatic worker (``factory watch``) ----------------------------------


def _stub_runtime() -> object:
    """A inert runtime stand-in: ``watch`` builds it but never dispatches here."""

    class StubRuntime:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def run_once(self) -> RuntimeResult:
            return RuntimeResult(
                None, None, None, None, None, None, None, "NO_ELIGIBLE_TASK", IntakeSummary()
            )

    return StubRuntime


def _install_fake_watcher(
    monkeypatch: pytest.MonkeyPatch, *, max_iterations: int, seen: dict[str, object]
) -> None:
    """Patch ``FactoryWatcher`` with a bounded, sleep-free stand-in.

    It still calls the injected runtime once per iteration, so the CLI wiring
    (runtime build, signal handlers, output) is exercised without a real loop.
    """

    class FakeWatcher:
        def __init__(
            self,
            *,
            runtime: object,
            idle_interval: float,
            max_iterations: int | None = None,
            sleep: object = None,
            should_stop: object = None,
        ) -> None:
            seen["idle_interval"] = idle_interval
            del max_iterations, sleep, should_stop
            self._runtime = runtime

        def run(self) -> WatchOutcome:
            last: RuntimeResult | None = None
            for _ in range(max_iterations):
                last = self._runtime.run_once()  # type: ignore[attr-defined]
            return WatchOutcome(
                iterations=max_iterations,
                processed=0,
                idle_waits=max_iterations,
                stopped=False,
                last_result=last,
            )

    monkeypatch.setattr(cli, "FactoryWatcher", FakeWatcher)


def test_watch_cli_builds_runtime_and_reports_outcome(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: dict[str, object] = {}

    class FakePool:
        def __init__(self, runtime_factory: object, **kwargs: object) -> None:
            del runtime_factory
            seen.update(kwargs)

        def run(self) -> None:
            return None

    monkeypatch.setattr(cli, "WorkerPool", FakePool)
    for key, value in _runtime_env(tmp_path, FACTORY_WATCH_IDLE_INTERVAL="3").items():
        monkeypatch.setenv(key, value)

    code = cli.main(["watch"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_OK
    assert TOKEN not in captured.out
    assert seen["idle_interval"] == 3.0
    assert seen["max_concurrency"] == 2


def test_watch_cli_fails_closed_without_write_token(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("GITHUB_WRITE_TOKEN", raising=False)
    for key, value in _runtime_env(tmp_path, write_token=None).items():
        monkeypatch.setenv(key, value)

    code = cli.main(["watch"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_CONFIG_ERROR
    assert "GITHUB_WRITE_TOKEN is required" in captured.out
    assert TOKEN not in captured.out


def test_watch_cli_does_not_echo_provider_secret(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    provider_secret = "watch-provider-secret-must-not-escape"

    class ExplodingPool:
        def __init__(self, runtime_factory: object, **kwargs: object) -> None:
            del runtime_factory, kwargs

        def run(self) -> None:
            raise RuntimeError(provider_secret)

    monkeypatch.setattr(cli, "WorkerPool", ExplodingPool)
    for key, value in _runtime_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    code = cli.main(["watch"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_INTAKE_ERROR
    assert "pool failed" in captured.out
    assert provider_secret not in captured.out
    assert provider_secret not in captured.err


def test_watch_cli_installs_and_restores_stop_handlers(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import signal

    recorded: list[tuple[int, object]] = []
    previous = {signal.SIGINT: "prev-int", signal.SIGTERM: "prev-term"}

    def fake_signal(signum: int, handler: object) -> object:
        recorded.append((signum, handler))
        return previous[signum]

    monkeypatch.setattr(cli.signal, "signal", fake_signal)

    class FakePool:
        def __init__(self, runtime_factory: object, **kwargs: object) -> None:
            del runtime_factory, kwargs

        def run(self) -> None:
            return None

    monkeypatch.setattr(cli, "WorkerPool", FakePool)
    for key, value in _runtime_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    assert cli.main(["watch"]) == cli.EXIT_OK

    installed = {signum for signum, handler in recorded if callable(handler)}
    restored = [(signum, handler) for signum, handler in recorded if not callable(handler)]
    assert installed == {signal.SIGINT, signal.SIGTERM}
    assert (signal.SIGINT, "prev-int") in restored
    assert (signal.SIGTERM, "prev-term") in restored


def test_installed_stop_handler_requests_a_cooperative_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import signal
    import threading

    handlers: dict[int, object] = {}

    def fake_signal(signum: int, handler: object) -> object:
        handlers[signum] = handler
        return signal.SIG_DFL

    monkeypatch.setattr(cli.signal, "signal", fake_signal)
    stop = threading.Event()
    cli._install_stop_handlers(stop)

    assert not stop.is_set()
    handlers[signal.SIGINT](signal.SIGINT, None)  # type: ignore[operator]
    assert stop.is_set()


def test_run_remains_one_shot_and_does_not_construct_a_worker_pool(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[str] = []

    class OneShotRuntime:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def run_once(self) -> RuntimeResult:
            calls.append("run_once")
            return RuntimeResult(
                "task-1",
                "run-1",
                TaskStatus.WAITING_HUMAN,
                "factory/task-1/run-1",
                None,
                7,
                "https://example.invalid/pr/7",
                "WAITING_HUMAN",
                IntakeSummary(),
            )

    class ForbiddenPool:
        def __init__(self, **kwargs: object) -> None:
            raise AssertionError("pool must not be constructed by `factory run`")

    monkeypatch.setattr(cli, "FactoryRuntime", OneShotRuntime)
    monkeypatch.setattr(cli, "WorkerPool", ForbiddenPool)
    for key, value in _runtime_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    code = cli.main(["run"])
    out = capsys.readouterr().out

    assert code == cli.EXIT_OK
    assert calls == ["run_once"]
    assert "Outcome: WAITING_HUMAN" in out


def test_run_reports_ineligible_source_as_success_without_secrets(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class StaleSourceRuntime:
        def __init__(self, **kwargs: object) -> None:
            del kwargs

        def run_once(self) -> RuntimeResult:
            return RuntimeResult(
                "task-1",
                None,
                TaskStatus.CANCELLED,
                None,
                None,
                None,
                None,
                "SOURCE_INELIGIBLE",
                IntakeSummary(),
            )

    monkeypatch.setattr(cli, "FactoryRuntime", StaleSourceRuntime)
    for key, value in _runtime_env(tmp_path).items():
        monkeypatch.setenv(key, value)

    code = cli.main(["run"])
    captured = capsys.readouterr()

    assert code == cli.EXIT_OK
    assert "Task status: CANCELLED" in captured.out
    assert "Outcome: SOURCE_INELIGIBLE" in captured.out
    assert TOKEN not in captured.out + captured.err
    assert "write-secret" not in captured.out + captured.err
