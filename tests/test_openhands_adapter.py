"""Tests for :class:`OpenHandsAdapter`.

The adapter is exercised against an injected in-memory transport, so dispatch,
collect and cancel run deterministically with no live server. The focus is the
mapping between factory domain types and the engine's contract, plus the error
boundary: no raw OpenHands payload or credential may escape.
"""

from __future__ import annotations

import traceback

import pytest

from factory.domain.enums import AgentKind, RunStatus
from factory.domain.models import AgentAdapter, AgentRun, FactoryTask, TaskSource, Workspace
from factory.integrations.openhands.adapter import OpenHandsAdapter
from factory.integrations.openhands.client import (
    OpenHandsClient,
    OpenHandsConnectionError,
    OpenHandsError,
    OpenHandsStatusError,
    ServerResponse,
)
from factory.integrations.openhands.execution import OpenHandsExecution
from tests.fake_openhands import FakeTransport

BASE_URL = "http://localhost:60000"
SESSION_KEY = "session-key-must-not-leak"
CONVERSATION_ID = "1f4b915f-c720-4b05-bd9b-093f7abdbc85"
WORKSPACE_PATH = "/var/lib/factory/workspaces/task-1"


def _task(**overrides: object) -> FactoryTask:
    defaults: dict[str, object] = {
        "title": "Add a health endpoint",
        "target_repository": "cartunduaga06/finanza-ia",
        "source": TaskSource("github", "cartunduaga06/ai-factory-lab", 42),
        "body": "Expose GET /health returning 200.",
    }
    defaults.update(overrides)
    return FactoryTask(**defaults)  # type: ignore[arg-type]


def _workspace() -> Workspace:
    return Workspace(
        repository_slug="cartunduaga06/finanza-ia",
        branch="factory/task-1",
        path=WORKSPACE_PATH,
    )


def _adapter(transport: FakeTransport) -> OpenHandsAdapter:
    client = OpenHandsClient(BASE_URL, session_api_key=SESSION_KEY, transport=transport)
    return OpenHandsAdapter(client, OpenHandsExecution(agent_profile_id="profile-1"))


# -- contract --------------------------------------------------------------


def test_adapter_satisfies_the_agent_adapter_protocol() -> None:
    adapter = _adapter(FakeTransport())
    assert isinstance(adapter, AgentAdapter)
    assert isinstance(adapter, OpenHandsAdapter)


def test_kind_is_openhands() -> None:
    assert _adapter(FakeTransport()).kind is AgentKind.OPENHANDS


# -- dispatch --------------------------------------------------------------


def test_dispatch_returns_a_run_bound_to_the_conversation() -> None:
    transport = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    task = _task()
    run = _adapter(transport).dispatch(task, _workspace())

    assert run.adapter is AgentKind.OPENHANDS
    assert run.run_id == CONVERSATION_ID
    assert run.task_id == task.task_id
    assert run.status is RunStatus.PENDING
    assert run.workspace is not None
    assert run.workspace.path == WORKSPACE_PATH
    assert run.started_at is not None
    assert run.finished_at is None


def test_dispatch_posts_to_the_conversations_endpoint() -> None:
    transport = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    _adapter(transport).dispatch(_task(), _workspace())
    assert transport.last.method == "POST"
    assert transport.last.url == f"{BASE_URL}/api/conversations"


def test_dispatch_sends_the_factory_workspace_path_and_task_text() -> None:
    import json

    transport = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    _adapter(transport).dispatch(_task(), _workspace())
    body = json.loads(transport.last.body.decode())
    assert body["workspace"] == {"kind": "LocalWorkspace", "working_dir": WORKSPACE_PATH}
    assert "Add a health endpoint" in body["initial_message"]["content"][0]["text"]
    assert body["agent_profile_id"] == "profile-1"


def test_dispatch_never_sends_its_own_llm_credential() -> None:
    import json

    transport = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    _adapter(transport).dispatch(_task(), _workspace())
    body = json.loads(transport.last.body.decode())
    assert "llm" not in body
    assert "api_key" not in json.dumps(body)


def test_dispatch_maps_a_running_conversation() -> None:
    transport = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "running"})]
    )
    run = _adapter(transport).dispatch(_task(), _workspace())
    assert run.status is RunStatus.RUNNING
    assert run.finished_at is None


def test_dispatch_closes_a_terminal_conversation_immediately() -> None:
    transport = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "finished"})]
    )
    run = _adapter(transport).dispatch(_task(), _workspace())
    assert run.status is RunStatus.SUCCEEDED
    assert run.finished_at is not None


def test_dispatch_response_without_id_fails() -> None:
    transport = FakeTransport([ServerResponse(201, {"execution_status": "idle"})])
    with pytest.raises(OpenHandsError):
        _adapter(transport).dispatch(_task(), _workspace())


def test_dispatch_with_an_unknown_status_fails_safely() -> None:
    transport = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "wat"})]
    )
    with pytest.raises(OpenHandsStatusError):
        _adapter(transport).dispatch(_task(), _workspace())


# -- collect ---------------------------------------------------------------


def _run(status: RunStatus = RunStatus.PENDING) -> AgentRun:
    return AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=status,
        workspace=_workspace(),
    )


@pytest.mark.parametrize(
    ("external", "expected", "terminal"),
    [
        ("idle", RunStatus.PENDING, False),
        ("running", RunStatus.RUNNING, False),
        ("paused", RunStatus.RUNNING, False),
        ("waiting_for_confirmation", RunStatus.RUNNING, False),
        ("finished", RunStatus.SUCCEEDED, True),
        ("error", RunStatus.FAILED, True),
        ("stuck", RunStatus.FAILED, True),
        ("deleting", RunStatus.CANCELLED, True),
    ],
)
def test_collect_maps_every_state(external: str, expected: RunStatus, terminal: bool) -> None:
    responses = [
        ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": external}),
        ServerResponse(200, {"response": "done"}),
    ]
    collected = _adapter(FakeTransport(responses)).collect(_run())
    assert collected.status is expected
    assert (collected.finished_at is not None) is terminal


def test_collect_running_leaves_the_run_open() -> None:
    transport = FakeTransport(
        [ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "running"})]
    )
    run = _adapter(transport).collect(_run())
    assert run.status is RunStatus.RUNNING
    assert run.finished_at is None


def test_collect_finished_captures_a_sanitized_summary() -> None:
    transport = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "finished"}),
            ServerResponse(200, {"response": "Implemented the endpoint."}),
        ]
    )
    run = _adapter(transport).collect(_run())
    assert run.status is RunStatus.SUCCEEDED
    assert run.summary == "Implemented the endpoint."
    assert run.finished_at is not None


def test_collect_scrubs_secrets_from_the_summary() -> None:
    transport = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "finished"}),
            ServerResponse(200, {"response": f"used {SESSION_KEY} and sk-leaked123"}),
        ]
    )
    run = _adapter(transport).collect(_run())
    assert run.summary is not None
    assert SESSION_KEY not in run.summary
    assert "sk-leaked123" not in run.summary


def test_collect_failed_still_marks_the_run_terminal() -> None:
    transport = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "error"}),
            ServerResponse(200, {"response": ""}),
        ]
    )
    run = _adapter(transport).collect(_run())
    assert run.status is RunStatus.FAILED
    assert run.finished_at is not None
    assert run.summary is None


def test_collect_cancelled_maps_deleting() -> None:
    transport = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "deleting"}),
            ServerResponse(404, {"detail": "gone"}),
        ]
    )
    run = _adapter(transport).collect(_run())
    assert run.status is RunStatus.CANCELLED
    assert run.finished_at is not None


def test_collect_unknown_state_fails_safely() -> None:
    transport = FakeTransport(
        [ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "banana"})]
    )
    with pytest.raises(OpenHandsStatusError):
        _adapter(transport).collect(_run())


def test_collect_missing_conversation_is_an_error() -> None:
    transport = FakeTransport([ServerResponse(404, {"detail": "gone"})])
    with pytest.raises(OpenHandsError) as caught:
        _adapter(transport).collect(_run())
    assert CONVERSATION_ID in str(caught.value)


def test_collect_does_not_put_raw_payloads_on_the_run() -> None:
    transport = FakeTransport(
        [
            ServerResponse(
                200,
                {
                    "id": CONVERSATION_ID,
                    "execution_status": "finished",
                    "stats": {"usage_to_metrics": {"secretish": "payload"}},
                },
            ),
            ServerResponse(200, {"response": "ok"}),
        ]
    )
    run = _adapter(transport).collect(_run())
    # The run is a domain object; nothing engine-specific is attached to it.
    serialized = repr(run)
    assert "usage_to_metrics" not in serialized
    assert "secretish" not in serialized
    assert "payload" not in serialized


# -- cancel ----------------------------------------------------------------


def test_cancel_requests_an_interrupt() -> None:
    transport = FakeTransport([ServerResponse(200, {"success": True})])
    _adapter(transport).cancel(_run(RunStatus.RUNNING))
    assert transport.last.method == "POST"
    assert transport.last.url == f"{BASE_URL}/api/conversations/{CONVERSATION_ID}/interrupt"


def test_cancel_is_idempotent_when_already_terminal() -> None:
    transport = FakeTransport([ServerResponse(200, {"success": True})])
    adapter = _adapter(transport)
    adapter.cancel(_run(RunStatus.SUCCEEDED))
    adapter.cancel(_run(RunStatus.FAILED))
    adapter.cancel(_run(RunStatus.CANCELLED))
    # A terminal run is not touched: no remote call is made.
    assert transport.requests == []


def test_cancel_tolerates_an_already_deleted_conversation() -> None:
    transport = FakeTransport([ServerResponse(404, {"detail": "gone"})])
    # Does not raise: already gone is treated as already cancelled.
    _adapter(transport).cancel(_run(RunStatus.RUNNING))


def test_cancel_twice_does_not_fail() -> None:
    transport = FakeTransport([ServerResponse(200, {"success": True})])
    adapter = _adapter(transport)
    adapter.cancel(_run(RunStatus.RUNNING))
    adapter.cancel(_run(RunStatus.RUNNING))
    assert len(transport.requests) == 2


def test_cancel_does_not_turn_an_api_error_into_success() -> None:
    transport = FakeTransport([ServerResponse(500, {"detail": "boom"})])
    with pytest.raises(OpenHandsError):
        _adapter(transport).cancel(_run(RunStatus.RUNNING))


def test_cancel_does_not_turn_a_transport_error_into_success() -> None:
    with pytest.raises(OpenHandsConnectionError):
        _adapter(FakeTransport(raise_on_send=True)).cancel(_run(RunStatus.RUNNING))


# -- error boundary --------------------------------------------------------


def test_engine_failure_does_not_leak_the_session_key() -> None:
    transport = FakeTransport(
        [ServerResponse(500, {"detail": f"upstream failed with {SESSION_KEY}"})]
    )
    with pytest.raises(OpenHandsError) as caught:
        _adapter(transport).dispatch(_task(), _workspace())
    formatted = "".join(traceback.format_exception(caught.value))
    assert SESSION_KEY not in formatted


def test_adapter_repr_is_credential_free() -> None:
    assert SESSION_KEY not in repr(_adapter(FakeTransport()))
