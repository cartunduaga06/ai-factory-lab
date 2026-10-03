"""Operator status is derived from persisted, allowlisted evidence."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs

from factory.__main__ import main
from factory.domain.enums import AgentKind, QualityGateStatus, RunStatus, TaskStatus
from factory.domain.models import (
    AgentRun,
    FactoryTask,
    PullRequest,
    QualityGate,
    StatusSnapshot,
    TaskSource,
)
from factory.infrastructure.config import FactoryConfig
from factory.infrastructure.persistence import (
    SqlitePullRequestRepository,
    SqliteRunRepository,
    SqliteTaskRepository,
)
from factory.infrastructure.persistence.audit import SqliteAuditEventStore
from factory.infrastructure.persistence.status_events import SqliteStatusEventStore
from factory.integrations.status_http import render_status
from factory.integrations.trello.status import TrelloStatusChannel
from factory.orchestration.status import StatusService
from factory.orchestration.status_events import StatusEventPublisher
from factory.orchestration.tracking import RunTrackingService
from tests.fake_adapter import FakeAgentAdapter


def _invoke_status_cli(database_url: str, port: int) -> None:
    with patch.dict(
        "os.environ",
        {"DATABASE_URL": database_url, "FACTORY_HEARTBEAT_INTERVAL": "180"},
        clear=True,
    ):
        main(["status", "--serve", "--host", "127.0.0.1", "--port", str(port)])


class _CapturedResponse:
    def __init__(self) -> None:
        self.headers: dict[str, str] = {}
        self.status: int | None = None
        self.body = b""

    def write(self, body: bytes) -> None:
        self.body = body

    def send_response(self, status: int) -> None:
        self.status = status

    def send_header(self, name: str, value: str) -> None:
        self.headers[name] = value

    def end_headers(self) -> None:
        return


class _InProcessStatusServer:
    handler: type
    instance: _InProcessStatusServer

    def __init__(self, address: tuple[str, int], handler: type) -> None:
        self.handler = handler
        self.instance = self

    def __enter__(self) -> _InProcessStatusServer:
        return self

    def __exit__(self, *args: object) -> None:
        return

    def serve_forever(self) -> None:
        for path in self.paths:
            request = self.handler.__new__(self.handler)
            request.path = path
            request.headers = {"Accept": "application/json"}
            request.wfile = _CapturedResponse()
            request.send_response = request.wfile.send_response
            request.send_header = request.wfile.send_header
            request.end_headers = request.wfile.end_headers
            request.do_GET()
            self.responses[path] = request.wfile

    paths: tuple[str, ...] = ()
    responses: dict[str, _CapturedResponse] = {}


class ControlTowerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        path = str(Path(self.temporary.name) / "fallback" / "factory.db")
        self.tasks = SqliteTaskRepository(path)
        self.runs = SqliteRunRepository(path)
        self.prs = SqlitePullRequestRepository(path)
        self.events = SqliteStatusEventStore(path)
        for repository in (self.tasks, self.runs, self.prs, self.events):
            repository.initialize()
        self.service = StatusService(
            self.tasks, self.runs, self.prs, heartbeat_interval=180, missed_heartbeats=2
        )

    def _task(self, status: TaskStatus) -> FactoryTask:
        task = FactoryTask(
            title="Secret-looking task title ghp_private",
            target_repository="example/project",
            source=TaskSource("github", "example/project", 53),
            status=status,
        )
        self.tasks.save(task)
        return task

    def _run(self, task: FactoryTask, *, heartbeat: datetime | None = None) -> AgentRun:
        run = AgentRun(
            task_id=task.task_id,
            adapter=AgentKind.CODEX,
            status=RunStatus.RUNNING,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
            last_heartbeat=heartbeat,
        )
        self.runs.save_run(run)
        return run

    def test_running_stall_healthy_heartbeat_and_recovery(self) -> None:
        task = self._task(TaskStatus.RUNNING)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        self._run(task, heartbeat=start)
        healthy = self.service.for_task(task.task_id, now=start + timedelta(seconds=359))
        self.assertEqual(healthy.phase, "RUNNING")
        stalled = self.service.for_task(task.task_id, now=start + timedelta(seconds=361))
        self.assertEqual(stalled.phase, "STALLED")
        self.assertEqual(stalled.evidence, "No verified heartbeat within the configured threshold")

    def test_codex_agent_liveness_is_separate_from_supervisor_heartbeat(self) -> None:
        task = self._task(TaskStatus.RUNNING)
        start = datetime(2026, 1, 1, tzinfo=UTC)
        run = self._run(task, heartbeat=start)
        unknown = self.service.for_task(task.task_id, now=start + timedelta(seconds=1))
        self.assertEqual(unknown.agent_liveness, "unknown")
        run.agent_heartbeat = start
        self.runs.update_run(run)
        alive = self.service.for_task(task.task_id, now=start + timedelta(seconds=5))
        self.assertEqual(alive.agent_liveness, "alive")
        stalled = self.service.for_task(task.task_id, now=start + timedelta(seconds=361))
        self.assertEqual(stalled.agent_liveness, "stalled")
        self.events.observe(stalled)
        self.events.observe(stalled)
        self.assertEqual(len(self.events.pending()), 1)
        run.last_heartbeat = start + timedelta(seconds=362)
        self.runs.update_run(run)
        recovered = self.service.for_task(task.task_id, now=start + timedelta(seconds=363))
        self.assertEqual(recovered.phase, "RUNNING")
        self.events.observe(recovered)
        self.assertEqual(len(self.events.pending()), 2)

    def test_status_http_reads_the_configured_database(self) -> None:
        configured_dir = Path(self.temporary.name) / "configured"
        configured_dir.mkdir()
        fallback_dir = Path(self.temporary.name) / "fallback"
        fallback_dir.mkdir(exist_ok=True)
        configured_path = f"sqlite:///{configured_dir / 'production.sqlite'}"
        database_path = FactoryConfig.from_env({"DATABASE_URL": configured_path}).database.path
        configured_tasks = SqliteTaskRepository(database_path)
        configured_runs = SqliteRunRepository(database_path)
        configured_prs = SqlitePullRequestRepository(database_path)
        for repository in (configured_tasks, configured_runs, configured_prs):
            repository.initialize()
        known_task = FactoryTask(
            title="Configured database task",
            target_repository="example/project",
            source=TaskSource("github", "example/project", 116),
            status=TaskStatus.READY,
        )
        configured_tasks.save(known_task)
        fallback_path = str(fallback_dir / "factory.db")
        fallback_tasks = SqliteTaskRepository(fallback_path)
        fallback_runs = SqliteRunRepository(fallback_path)
        fallback_prs = SqlitePullRequestRepository(fallback_path)
        for repository in (fallback_tasks, fallback_runs, fallback_prs):
            repository.initialize()
        fallback_task = FactoryTask(
            title="Fallback database task",
            target_repository="example/project",
            source=TaskSource("github", "example/project", 999),
            status=TaskStatus.READY,
        )
        fallback_tasks.save(fallback_task)
        audit = SqliteAuditEventStore(database_path)
        known_event = audit.for_task(known_task.task_id)[0]

        original_cwd = Path.cwd()
        os.chdir(fallback_dir)
        self.addCleanup(os.chdir, original_cwd)
        _InProcessStatusServer.paths = (
            f"/factory/status?task_id={known_task.task_id}",
            f"/factory/trace?task_id={known_task.task_id}",
        )
        _InProcessStatusServer.responses = {}
        with patch("factory.integrations.status_http.ThreadingHTTPServer", _InProcessStatusServer):
            _invoke_status_cli(configured_path, 18765)
        status_response = next(
            response
            for path, response in _InProcessStatusServer.responses.items()
            if path.startswith("/factory/status")
        )
        trace_response = next(
            response
            for path, response in _InProcessStatusServer.responses.items()
            if path.startswith("/factory/trace")
        )
        self.assertEqual(status_response.status, 200)
        self.assertEqual(trace_response.status, 200)
        status_body = status_response.body.decode()
        self.assertIn(known_task.task_id, status_body)
        self.assertNotIn(fallback_task.task_id, status_body)
        trace_body = trace_response.body.decode()
        self.assertIn(known_event.name, trace_body)
        self.assertNotIn(fallback_task.task_id, trace_body)

    def test_status_startup_is_read_only_and_missing_database_is_not_created(self) -> None:
        configured_dir = Path(self.temporary.name) / "startup"
        configured_dir.mkdir()
        database_path = configured_dir / "existing.sqlite"
        tasks = SqliteTaskRepository(str(database_path))
        runs = SqliteRunRepository(str(database_path))
        prs = SqlitePullRequestRepository(str(database_path))
        for repository in (tasks, runs, prs):
            repository.initialize()
        task = FactoryTask(
            title="Read-only startup task",
            target_repository="example/project",
            source=TaskSource("github", "example/project", 118),
            status=TaskStatus.READY,
        )
        tasks.save(task)
        before = database_path.read_bytes()
        before_stat = database_path.stat()
        original_cwd = Path.cwd()
        os.chdir(configured_dir)
        self.addCleanup(os.chdir, original_cwd)
        _InProcessStatusServer.paths = (f"/factory/status?task_id={task.task_id}",)
        _InProcessStatusServer.responses = {}
        with patch("factory.integrations.status_http.ThreadingHTTPServer", _InProcessStatusServer):
            _invoke_status_cli(f"sqlite:///{database_path}", 18766)
        self.assertEqual(database_path.read_bytes(), before)
        self.assertEqual(database_path.stat().st_mtime_ns, before_stat.st_mtime_ns)
        response = next(iter(_InProcessStatusServer.responses.values()))
        self.assertIn(task.task_id, response.body.decode())

        missing = configured_dir / "missing.sqlite"
        _InProcessStatusServer.paths = ()
        with patch.dict("os.environ", {"DATABASE_URL": f"sqlite:///{missing}"}, clear=True):
            self.assertNotEqual(main(["status", "--serve", "--port", "18767"]), 0)
        self.assertFalse(missing.exists())

    def test_read_only_status_rejects_legacy_schema_without_migrating(self) -> None:
        path = Path(self.temporary.name) / "legacy-status.sqlite"
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE tasks (task_id TEXT PRIMARY KEY)")
        before = path.read_bytes()
        with patch.dict("os.environ", {"DATABASE_URL": f"sqlite:///{path}"}, clear=True):
            self.assertNotEqual(main(["status", "--serve", "--port", "18768"]), 0)
        self.assertEqual(path.read_bytes(), before)

    def test_status_service_unit_contract_is_loopback_only(self) -> None:
        unit = Path("ops/control-tower/systemd/factory-control-tower.service").read_text()
        self.assertIn("--host 127.0.0.1", unit)
        self.assertNotIn("--host 0.0.0.0", unit)

    def test_config_masks_trello_credentials_and_bounds_heartbeat(self) -> None:
        config = FactoryConfig.from_env(
            {
                "FACTORY_HEARTBEAT_INTERVAL": "9999",
                "FACTORY_MISSED_HEARTBEATS": "3",
                "FACTORY_TRELLO_KEY": "key-secret",
                "FACTORY_TRELLO_TOKEN": "token-secret",
            }
        )
        self.assertEqual(config.heartbeat_interval, 300.0)
        self.assertEqual(config.missed_heartbeats, 3)
        self.assertNotIn("key-secret", repr(config.redacted()))
        self.assertNotIn("token-secret", repr(config.redacted()))

    def test_phone_view_escapes_dynamic_content(self) -> None:
        page = render_status(StatusSnapshot(phase="<script>", evidence="<img src=x>"))
        self.assertIn('name="viewport"', page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn("<img src=x>", page)

    def test_gate_evidence_is_persisted_and_sanitized_in_control_tower(self) -> None:
        task = self._task(TaskStatus.READY)
        run = self._run(task)
        run.status = RunStatus.SUCCEEDED
        run.gates = (
            QualityGate("ruff_check", QualityGateStatus.FAILED, "exit_code=1"),
            QualityGate("mypy", QualityGateStatus.PASSED, "secret=ghp_private"),
        )
        self.runs.update_run(run)
        snapshot = self.service.for_task(task.task_id)
        self.assertEqual(self.service.current().phase, "READY")
        self.assertEqual(snapshot.action, "QA rework queued")
        self.assertEqual(snapshot.evidence, "1 required check(s) did not pass")
        page = render_status(snapshot)
        self.assertEqual(
            snapshot.gates,
            ("ruff_check: FAILED (required) (exit_code=1)", "mypy: PASSED (required)"),
        )
        self.assertIn("ruff_check: FAILED", page)
        self.assertNotIn("ghp_private", page)

    def test_previous_gate_results_remain_visible_after_correction(self) -> None:
        task = self._task(TaskStatus.WAITING_HUMAN)
        prior = self._run(task)
        prior.status = RunStatus.SUCCEEDED
        prior.gates = (QualityGate("ruff_check", QualityGateStatus.FAILED, "exit_code=1"),)
        self.runs.update_run(prior)
        latest = self._run(task)
        latest.status = RunStatus.SUCCEEDED
        latest.gates = (QualityGate("ruff_check", QualityGateStatus.PASSED, "exit_code=0"),)
        self.runs.update_run(latest)
        snapshot = self.service.for_task(task.task_id)
        self.assertEqual(snapshot.gates, ("ruff_check: PASSED (required) (exit_code=0)",))
        self.assertEqual(len(snapshot.previous_gates), 1)
        self.assertIn("ruff_check: FAILED", snapshot.previous_gates[0])
        self.assertIn("Previous gate results", render_status(snapshot))

    def test_successful_collection_persists_heartbeat_without_event(self) -> None:
        task = self._task(TaskStatus.RUNNING)
        stale = datetime.now(UTC) - timedelta(minutes=20)
        run = self._run(task, heartbeat=stale)
        adapter = FakeAgentAdapter(kind=AgentKind.CODEX, collect_status=RunStatus.RUNNING)
        tracker = RunTrackingService(self.tasks, self.runs, heartbeat_interval=180)
        tracker.refresh(run.run_id, adapter)
        refreshed = self.runs.get_run(run.run_id)
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertGreater(refreshed.last_heartbeat, stale)
        self.assertEqual(self.service.for_task(task.task_id).phase, "RUNNING")
        self.assertEqual(self.events.pending(), [])

    def test_existing_run_table_gains_heartbeat_column(self) -> None:
        path = str(Path(self.temporary.name) / "legacy.db")
        with sqlite3.connect(path) as connection:
            connection.execute(
                "CREATE TABLE agent_runs (run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL, "
                "adapter TEXT NOT NULL, status TEXT NOT NULL, workspace_id TEXT, "
                "summary TEXT, started_at TEXT, finished_at TEXT, "
                "gates TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL)"
            )
        SqliteRunRepository(path).initialize()
        with sqlite3.connect(path) as connection:
            columns = [row[1] for row in connection.execute("PRAGMA table_info(agent_runs)")]
        self.assertIn("last_heartbeat", columns)

    def test_waiting_human_done_and_sanitized_failure(self) -> None:
        task = self._task(TaskStatus.WAITING_HUMAN)
        run = self._run(task)
        self.prs.save(
            PullRequest(
                repository_slug="example/project",
                head_branch="factory/task/run",
                base_branch="main",
                title="ghp_private",
                number=17,
                url="https://evil.example/ghp_private",
                task_id=task.task_id,
                run_id=run.run_id,
            )
        )
        waiting = self.service.for_task(task.task_id)
        self.assertEqual(waiting.action, "Review the pull request")
        self.assertEqual(waiting.pr_url, "https://github.com/example/project/pull/17")
        self.assertNotIn("ghp_private", render_status(waiting))
        task.status = TaskStatus.DONE
        self.tasks.update(task)
        self.assertEqual(self.service.for_task(task.task_id).phase, "DONE")
        task.status = TaskStatus.FAILED
        self.tasks.update(task)
        run.status = RunStatus.FAILED
        run.summary = "token=ghp_private"
        self.runs.update_run(run)
        failure = self.service.for_task(task.task_id)
        self.assertEqual(failure.evidence, "Agent reported failure")
        self.assertNotIn("ghp_private", render_status(failure))

    def test_status_includes_routing_published_commit_and_safe_blocked_action(self) -> None:
        task = self._task(TaskStatus.BLOCKED)
        task.blocked_reason = "security review blocked publication: token=ghp_private"
        self.tasks.update(task)
        run = self._run(task)
        self.runs.update_run(run)
        self.prs.save(
            PullRequest(
                repository_slug="example/project",
                head_branch="factory/task/run",
                base_branch="main",
                title="work",
                number=18,
                task_id=task.task_id,
                run_id=run.run_id,
                commit_sha="a" * 40,
            )
        )
        snapshot = self.service.for_task(task.task_id)
        self.assertEqual(snapshot.project_id, task.project_id)
        self.assertEqual(snapshot.repository, "example/project")
        self.assertEqual(snapshot.commit_sha, "a" * 40)
        self.assertEqual(snapshot.blocked_reason, "Blocked; inspect the task record")
        self.assertEqual(snapshot.action, "Review the blocking reason")
        page = render_status(snapshot)
        self.assertIn("example/project", page)
        self.assertIn("Review the blocking reason", page)
        self.assertNotIn("ghp_private", page)

    def test_transition_outbox_and_trello_payload_exclude_secret_prose(self) -> None:
        task = self._task(TaskStatus.CLAIMED)
        self.tasks.apply_transition(task.task_id, TaskStatus.CLAIMED, TaskStatus.RUNNING)
        self.assertEqual(len(self.events.pending()), 1)
        requests = []
        channel = TrelloStatusChannel("abc123", "key-secret", "token-secret", send=requests.append)
        publisher = StatusEventPublisher(self.service, self.events, channel)
        publisher.flush()
        publisher.flush()
        self.assertEqual(len(requests), 1)
        request = requests[0]
        self.assertEqual(request.full_url, "https://api.trello.com/1/cards/abc123")
        payload = parse_qs(request.data.decode("utf-8"))
        self.assertNotIn("ghp_private", payload["desc"][0])
        self.assertEqual(self.events.pending(), [])

    def test_trello_alert_comment_is_filtered_and_sanitized(self) -> None:
        requests = []
        channel = TrelloStatusChannel("abc123", "key-secret", "token-secret", send=requests.append)
        channel.alert(StatusSnapshot(phase="RUNNING", evidence="heartbeat"))
        self.assertEqual(requests, [])
        channel.alert(StatusSnapshot(phase="FAILED", evidence="Agent reported failure"))
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].get_method(), "POST")
        payload = parse_qs(requests[0].data.decode("utf-8"))
        self.assertEqual(payload["text"], ["Factory alert: FAILED\nAgent reported failure"])

    def test_alert_channel_receives_only_actionable_transitions(self) -> None:
        task = self._task(TaskStatus.CLAIMED)
        self.tasks.apply_transition(task.task_id, TaskStatus.CLAIMED, TaskStatus.RUNNING)
        snapshots = []

        class Channel:
            def sync(self, snapshot: StatusSnapshot) -> None:
                snapshots.append(snapshot.phase)

        class Alerts:
            def alert(self, snapshot: StatusSnapshot) -> None:
                alerts.append(snapshot.phase)

        alerts: list[str] = []
        publisher = StatusEventPublisher(self.service, self.events, Channel(), Alerts())
        publisher.flush()
        self.assertEqual(alerts, [])
        self.tasks.apply_transition(task.task_id, TaskStatus.RUNNING, TaskStatus.WAITING_HUMAN)
        publisher.flush()
        self.assertEqual(alerts, ["WAITING_HUMAN"])
        publisher.flush()
        self.assertEqual(alerts, ["WAITING_HUMAN"])
        self.assertEqual(snapshots, ["RUNNING", "WAITING_HUMAN"])


if __name__ == "__main__":
    unittest.main()
