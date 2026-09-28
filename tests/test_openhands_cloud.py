"""Tests for :class:`OpenHandsCloudAdapter` and :class:`CloudControlClient`.

The Cloud control API and the sandbox agent-server are both driven through
injected in-memory transports, so every path — successful dispatch/collect,
active status, terminal failure/cancel, malformed provider responses, transport
failure and idempotent re-collection — runs deterministically with no network.

The focus is the safety contract: the Cloud API key never escapes, provider
payloads are sanitized at the boundary, re-collection never creates a second
sandbox or conversation, and a result that cannot be independently validated
locally is failed closed rather than published.
"""

from __future__ import annotations

import json

import pytest

from factory.domain.enums import AgentKind, RunStatus
from factory.domain.models import (
    AgentAdapter,
    AgentRun,
    FactoryTask,
    RemoteRevision,
    TaskSource,
    Workspace,
)
from factory.integrations.openhands.client import (
    OpenHandsConnectionError,
    OpenHandsError,
    ServerResponse,
)
from factory.integrations.openhands.cloud import (
    CloudControlClient,
    CloudExecution,
    OpenHandsCloudAdapter,
    OpenHandsCloudConfigurationError,
    OpenHandsCloudError,
    OpenHandsCloudRequestError,
    OpenHandsCloudResponseError,
    split_provider_ref,
)
from factory.integrations.workspace.cloud_revision import (
    CloudRevisionError,
    CloudRevisionProvider,
)
from tests.fake_openhands import FakeTransport

API_KEY = "cloud-api-key-must-not-leak"
PROFILE_ID = "d6934b00-ab23-4aed-bf84-14463e77f6e8"
SANDBOX_ID = "sbx-0f8e"
CONVERSATION_ID = "conv-7c21"
AGENT_SERVER_URL = "https://sbx-0f8e.agent.all-hands.dev"
REMOTE_SHA = "0123456789abcdef0123456789abcdef01234567"
WORKING_DIR = "/workspace/project"
REPOSITORY = "cartunduaga06/ai-factory-lab"


def _task(**overrides: object) -> FactoryTask:
    defaults: dict[str, object] = {
        "title": "Add a health endpoint",
        "target_repository": REPOSITORY,
        "source": TaskSource("github", REPOSITORY, 15),
        "body": "Expose GET /health returning 200.",
    }
    defaults.update(overrides)
    return FactoryTask(**defaults)  # type: ignore[arg-type]


def _workspace() -> Workspace:
    return Workspace(
        workspace_id="ws-cloud-1",
        repository_slug=REPOSITORY,
        branch="factory/task-1/ws-cloud-1",
        path="/var/lib/factory/workspaces/ws-cloud-1",
    )


def _running_sandbox_entry(status: str = "RUNNING") -> dict[str, object]:
    return {
        "id": SANDBOX_ID,
        "status": status,
        "session_api_key": "sandbox-session-key",
        "exposed_urls": [{"name": "AGENT_SERVER", "url": AGENT_SERVER_URL}],
    }


class _RecordingRevisionProvider(CloudRevisionProvider):
    """A real provider implementation that records what it was asked to do."""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[RemoteRevision] = []

    def materialize(self, workspace: Workspace, revision: RemoteRevision) -> None:
        self.calls.append(revision)
        if self.fail:
            raise CloudRevisionError(workspace.workspace_id)


def _control(transport: FakeTransport) -> CloudControlClient:
    return CloudControlClient("https://app.all-hands.dev", api_key=API_KEY, transport=transport)


def _execution() -> CloudExecution:
    return CloudExecution(
        working_dir=WORKING_DIR,
        repository=REPOSITORY,
        profile=PROFILE_ID,
        base_ref="main",
    )


def _adapter(
    control_transport: FakeTransport,
    conversation_transport: FakeTransport,
    revision: CloudRevisionProvider | None = None,
) -> OpenHandsCloudAdapter:
    return OpenHandsCloudAdapter(
        _control(control_transport),
        _execution(),
        revision or _RecordingRevisionProvider(),
        conversation_transport=conversation_transport,
        sleep=lambda _seconds: None,
    )


# -- contract --------------------------------------------------------------


def test_adapter_satisfies_the_agent_adapter_protocol() -> None:
    adapter = _adapter(FakeTransport(), FakeTransport())
    assert isinstance(adapter, AgentAdapter)
    assert adapter.kind is AgentKind.OPENHANDS


def test_control_client_repr_never_exposes_the_api_key() -> None:
    client = _control(FakeTransport())
    assert API_KEY not in repr(client)
    assert API_KEY not in client.api_url


def test_control_client_requires_a_credential() -> None:
    with pytest.raises(OpenHandsCloudConfigurationError):
        CloudControlClient("https://app.all-hands.dev", api_key="")


# -- dispatch --------------------------------------------------------------


def test_dispatch_returns_a_run_bound_to_the_conversation_and_sandbox() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, {"id": SANDBOX_ID, "session_api_key": "k"}),
            ServerResponse(200, [_running_sandbox_entry()]),
        ]
    )
    conversation = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    task = _task()
    run = _adapter(control, conversation).dispatch(task, _workspace())

    assert run.adapter is AgentKind.OPENHANDS
    assert run.run_id == CONVERSATION_ID
    assert run.provider_ref == f"{SANDBOX_ID}:{CONVERSATION_ID}"
    assert run.status is RunStatus.PENDING
    assert run.started_at is not None
    assert run.finished_at is None


def test_dispatch_posts_the_factory_branch_and_working_dir() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, {"id": SANDBOX_ID}),
            ServerResponse(200, [_running_sandbox_entry()]),
        ]
    )
    conversation = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    _adapter(control, conversation).dispatch(_task(), _workspace())

    body = json.loads(conversation.last.body.decode())
    assert body["workspace"] == {"kind": "LocalWorkspace", "working_dir": WORKING_DIR}
    assert body["agent_profile_id"] == PROFILE_ID
    assert conversation.last.url == f"{AGENT_SERVER_URL}/api/conversations"
    text = body["initial_message"]["content"][0]["text"]
    assert "Add a health endpoint" in text
    # The run's isolated branch travels in the instruction, not as a non-contract
    # conversation field the agent-server would reject.
    assert "factory/task-1/ws-cloud-1" in text
    assert "git" not in body


def test_dispatch_payload_uses_only_fields_the_conversation_contract_accepts() -> None:
    """The payload must not carry keys outside ``StartConversationRequest``.

    The agent-server conversation model forbids extra fields, so an invented key
    would be rejected in production even though an in-memory transport accepts it.
    """
    from factory.integrations.openhands.cloud import build_cloud_creation_payload

    body = build_cloud_creation_payload(
        _task(),
        _execution(),
        _workspace(),
        github_secret={
            "kind": "LookupSecret",
            "url": "https://example.invalid/secret",
            "headers": {},
        },
    ).as_dict()
    allowed = {
        "workspace",
        "confirmation_policy",
        "max_iterations",
        "stuck_detection",
        "autotitle",
        "initial_message",
        "agent_profile_id",
        "agent_settings",
        "secrets_encrypted",
        "secrets",
    }
    assert set(body) <= allowed


def test_dispatch_authenticates_to_the_control_api_with_bearer_and_never_in_the_url() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, {"id": SANDBOX_ID}),
            ServerResponse(200, [_running_sandbox_entry()]),
        ]
    )
    conversation = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    _adapter(control, conversation).dispatch(_task(), _workspace())

    create = control.requests[0]
    assert create.headers["Authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in create.url


def test_dispatch_uses_the_sandbox_session_key_for_the_conversation() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, {"id": SANDBOX_ID}),
            ServerResponse(200, [_running_sandbox_entry()]),
        ]
    )
    conversation = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    _adapter(control, conversation).dispatch(_task(), _workspace())
    assert conversation.requests[-1].headers.get("X-Session-API-Key") == "sandbox-session-key"


def test_dispatch_fails_closed_when_the_sandbox_never_becomes_ready() -> None:
    control = FakeTransport(
        [ServerResponse(200, {"id": SANDBOX_ID})],
        default=ServerResponse(200, [_running_sandbox_entry("STARTING")]),
    )
    conversation = FakeTransport()
    adapter = OpenHandsCloudAdapter(
        _control(control),
        _execution(),
        _RecordingRevisionProvider(),
        conversation_transport=conversation,
        ready_attempts=3,
        sleep=lambda _seconds: None,
    )
    with pytest.raises(OpenHandsCloudError):
        adapter.dispatch(_task(), _workspace())
    # No conversation was ever created.
    assert conversation.requests == []


def test_dispatch_releases_the_sandbox_when_conversation_creation_fails() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, {"id": SANDBOX_ID}),
            ServerResponse(200, [_running_sandbox_entry()]),
        ]
    )
    conversation = FakeTransport([ServerResponse(500, {"detail": "boom"})])
    # The shared Agent Server client raises its sanitized error; the Cloud adapter
    # reuses that contract rather than inventing a second one.
    with pytest.raises(OpenHandsError):
        _adapter(control, conversation).dispatch(_task(), _workspace())
    # The last control call released the sandbox.
    assert control.last.method == "DELETE"
    assert SANDBOX_ID in control.last.url


def test_dispatch_sanitizes_a_malformed_conversation_response() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, {"id": SANDBOX_ID}),
            ServerResponse(200, [_running_sandbox_entry()]),
        ]
    )
    conversation = FakeTransport([ServerResponse(201, {"no_id": True})])
    with pytest.raises((OpenHandsCloudResponseError, OpenHandsCloudError)) as caught:
        _adapter(control, conversation).dispatch(_task(), _workspace())
    assert API_KEY not in str(caught.value)


def test_dispatch_fails_when_the_control_api_is_unreachable() -> None:
    control = FakeTransport(raise_on_send=True)
    with pytest.raises(OpenHandsConnectionError):
        _adapter(control, FakeTransport()).dispatch(_task(), _workspace())


def test_dispatch_payload_injects_github_lookup_secret_without_raw_token() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, {"id": SANDBOX_ID, "session_api_key": "k"}),
            ServerResponse(200, [_running_sandbox_entry()]),
        ]
    )
    conversation = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    _adapter(control, conversation).dispatch(_task(), _workspace())
    body = json.loads(conversation.last.body.decode())
    secret = body["secrets"]["GITHUB_TOKEN"]
    assert secret["kind"] == "LookupSecret"
    assert secret["url"].endswith(f"/sandboxes/{SANDBOX_ID}/settings/secrets/github_token")
    assert secret["headers"]["X-Session-API-Key"] == "sandbox-session-key"
    assert API_KEY not in json.dumps(body)
    instruction = body["initial_message"]["content"][0]["text"]
    assert "GIT_ASKPASS" in instruction
    assert "$GITHUB_TOKEN" in instruction
    assert "https://github.com/cartunduaga06/ai-factory-lab.git" in instruction
    assert "main" in instruction


def test_cloud_execution_requires_an_agent_profile_uuid() -> None:
    with pytest.raises(ValueError, match="Agent Profile UUID"):
        CloudExecution(
            working_dir=WORKING_DIR,
            repository=REPOSITORY,
            profile="deepseek-v4.1-flash",
            base_ref="main",
        )


def test_await_ready_resumes_a_paused_sandbox() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, {"id": SANDBOX_ID, "session_api_key": "k"}),
            ServerResponse(200, [_running_sandbox_entry("PAUSED")]),
            ServerResponse(200, {}),
            ServerResponse(200, [_running_sandbox_entry("RUNNING")]),
        ]
    )
    conversation = FakeTransport(
        [ServerResponse(201, {"id": CONVERSATION_ID, "execution_status": "idle"})]
    )
    run = _adapter(control, conversation).dispatch(_task(), _workspace())
    assert run.run_id == CONVERSATION_ID
    assert any("/resume" in request.url for request in control.requests)


# -- collect ---------------------------------------------------------------


def _active_run(
    provider: CloudRevisionProvider | None = None,
) -> tuple[FakeTransport, FakeTransport, AgentRun]:
    control = FakeTransport([ServerResponse(200, [_running_sandbox_entry()])])
    conversation = FakeTransport()
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    return control, conversation, run


def test_collect_maps_an_active_status_without_materialising() -> None:
    provider = _RecordingRevisionProvider()
    _control_t, conversation, run = _active_run(provider)
    conversation._responses = [
        ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "running"})
    ]
    adapter = _adapter(
        FakeTransport([ServerResponse(200, [_running_sandbox_entry()])]), conversation, provider
    )
    result = adapter.collect(run)
    assert result.status is RunStatus.RUNNING
    assert result.finished_at is None
    assert provider.calls == []


def test_collect_materialises_and_marks_succeeded_on_a_verified_revision() -> None:
    provider = _RecordingRevisionProvider()
    control = FakeTransport([ServerResponse(200, [_running_sandbox_entry()])])
    conversation = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "finished"}),
            ServerResponse(200, {"commits": [{"sha": REMOTE_SHA}]}),
            ServerResponse(200, {"response": "Implemented the endpoint."}),
        ]
    )
    adapter = _adapter(control, conversation, provider)
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    result = adapter.collect(run)

    assert result.status is RunStatus.SUCCEEDED
    assert result.finished_at is not None
    assert result.summary == "Implemented the endpoint."
    assert provider.calls == [RemoteRevision(REMOTE_SHA, _workspace().branch, REPOSITORY)]


def test_collect_fails_closed_when_the_revision_cannot_be_materialised() -> None:
    provider = _RecordingRevisionProvider(fail=True)
    control = FakeTransport([ServerResponse(200, [_running_sandbox_entry()])])
    conversation = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "finished"}),
            ServerResponse(200, {"commits": [{"sha": REMOTE_SHA}]}),
            ServerResponse(200, {"response": "done"}),
        ]
    )
    adapter = _adapter(control, conversation, provider)
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    result = adapter.collect(run)
    assert result.status is RunStatus.FAILED
    # A failed run has no validated revision, so publication is impossible.
    assert result.validated_revision is None


def test_collect_fails_closed_when_the_sandbox_reports_no_commit() -> None:
    provider = _RecordingRevisionProvider()
    control = FakeTransport([ServerResponse(200, [_running_sandbox_entry()])])
    conversation = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "finished"}),
            ServerResponse(200, {"commits": []}),
        ]
    )
    adapter = _adapter(control, conversation, provider)
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    result = adapter.collect(run)
    assert result.status is RunStatus.FAILED
    assert provider.calls == []


@pytest.mark.parametrize(
    ("external", "expected"),
    [
        ("error", RunStatus.FAILED),
        ("stuck", RunStatus.FAILED),
        ("deleting", RunStatus.CANCELLED),
    ],
)
def test_collect_maps_terminal_failure_and_cancel(external: str, expected: RunStatus) -> None:
    provider = _RecordingRevisionProvider()
    control = FakeTransport([ServerResponse(200, [_running_sandbox_entry()])])
    conversation = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": external}),
            ServerResponse(200, {"response": "maybe"}),
        ]
    )
    adapter = _adapter(control, conversation, provider)
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    result = adapter.collect(run)
    assert result.status is expected
    assert provider.calls == []


def test_collect_is_idempotent_and_never_recreates_a_conversation() -> None:
    """Re-collection must not POST a new conversation; only GET on the existing one."""
    provider = _RecordingRevisionProvider()
    conversation = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "finished"}),
            ServerResponse(200, {"commits": [{"sha": REMOTE_SHA}]}),
            ServerResponse(200, {"response": "done"}),
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "finished"}),
            ServerResponse(200, {"commits": [{"sha": REMOTE_SHA}]}),
            ServerResponse(200, {"response": "done"}),
        ]
    )
    control = FakeTransport(
        [
            ServerResponse(200, [_running_sandbox_entry()]),
            ServerResponse(200, [_running_sandbox_entry()]),
        ]
    )
    adapter = _adapter(control, conversation, provider)
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    adapter.collect(run)
    adapter.collect(run)
    assert all(request.method == "GET" for request in conversation.requests)
    assert provider.calls == [RemoteRevision(REMOTE_SHA, _workspace().branch, REPOSITORY)] * 2


def test_collect_fails_closed_on_a_missing_provider_handle() -> None:
    adapter = _adapter(FakeTransport(), FakeTransport())
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=None,
    )
    with pytest.raises(OpenHandsCloudError):
        adapter.collect(run)


def test_collect_fails_closed_on_a_malformed_provider_handle() -> None:
    adapter = _adapter(FakeTransport(), FakeTransport())
    for handle in ["only-one-part", ":", "a:", ":b"]:
        run = AgentRun(
            task_id=_task().task_id,
            adapter=AgentKind.OPENHANDS,
            run_id=CONVERSATION_ID,
            status=RunStatus.RUNNING,
            workspace=_workspace(),
            provider_ref=handle,
        )
        with pytest.raises(OpenHandsCloudError):
            adapter.collect(run)


def test_collect_fails_when_the_sandbox_cannot_be_located() -> None:
    control = FakeTransport([ServerResponse(404, {"detail": "gone"})])
    adapter = _adapter(control, FakeTransport())
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    with pytest.raises(OpenHandsCloudError):
        adapter.collect(run)


def test_collect_sanitizes_a_leaky_provider_summary() -> None:
    provider = _RecordingRevisionProvider()
    control = FakeTransport([ServerResponse(200, [_running_sandbox_entry()])])
    conversation = FakeTransport(
        [
            ServerResponse(200, {"id": CONVERSATION_ID, "execution_status": "finished"}),
            ServerResponse(200, {"commits": [{"sha": REMOTE_SHA}]}),
            ServerResponse(200, {"response": f"used {API_KEY} and the sandbox key"}),
        ]
    )
    adapter = _adapter(control, conversation, provider)
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    result = adapter.collect(run)
    assert result.summary is not None
    assert API_KEY not in result.summary


# -- cancel ----------------------------------------------------------------


def test_cancel_interrupts_the_conversation_and_releases_the_sandbox() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, [_running_sandbox_entry()]),
            ServerResponse(200, {}),
        ]
    )
    conversation = FakeTransport([ServerResponse(200, {})])
    adapter = _adapter(control, conversation)
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    adapter.cancel(run)
    assert any(request.method == "DELETE" for request in control.requests)
    assert conversation.requests[-1].method == "POST"
    assert conversation.requests[-1].url.endswith(f"/{CONVERSATION_ID}/interrupt")


def test_cancel_is_idempotent_for_a_terminal_run() -> None:
    control = FakeTransport()
    adapter = _adapter(control, FakeTransport())
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.SUCCEEDED,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    adapter.cancel(run)
    assert control.requests == []


def test_cancel_tolerates_a_vanished_sandbox() -> None:
    control = FakeTransport([ServerResponse(404, {"detail": "gone"})])
    adapter = _adapter(control, FakeTransport())
    run = AgentRun(
        task_id=_task().task_id,
        adapter=AgentKind.OPENHANDS,
        run_id=CONVERSATION_ID,
        status=RunStatus.RUNNING,
        workspace=_workspace(),
        provider_ref=f"{SANDBOX_ID}:{CONVERSATION_ID}",
    )
    adapter.cancel(run)  # no exception


# -- provider handle helpers ----------------------------------------------


def test_split_provider_ref_round_trips() -> None:
    assert split_provider_ref("sbx:conv", "run") == ("sbx", "conv")


def test_control_request_error_carries_only_the_status() -> None:
    error = OpenHandsCloudRequestError(502)
    assert error.status == 502
    assert "502" in str(error)
    assert API_KEY not in str(error)


def test_execution_rejects_a_blank_working_directory_or_bad_repository() -> None:
    with pytest.raises(ValueError):
        CloudExecution(working_dir="  ", repository=REPOSITORY, profile=PROFILE_ID, base_ref="main")
    with pytest.raises(ValueError):
        CloudExecution(
            working_dir=WORKING_DIR, repository="not-a-slug", profile=PROFILE_ID, base_ref="main"
        )
