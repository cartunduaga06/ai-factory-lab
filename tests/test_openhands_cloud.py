"""Cloud sandbox exchange through injected, network-free transports."""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import pytest

from factory.domain.enums import AgentKind, RunStatus
from factory.domain.models import AgentRun, FactoryTask, RemoteRevision, TaskSource, Workspace
from factory.integrations.openhands.client import ServerResponse
from factory.integrations.openhands.cloud import (
    CloudControlClient,
    CloudExecution,
    OpenHandsCloudAdapter,
    OpenHandsCloudError,
    build_cloud_creation_payload,
    split_provider_ref,
)
from factory.integrations.openhands.cloud_files import BashResult
from factory.integrations.workspace.cloud_revision import CloudRevisionError
from tests.fake_openhands import FakeTransport

SHA = "0123456789abcdef0123456789abcdef01234567"
RESULT = "abcdef0123456789abcdef0123456789abcdef01"
PROFILE = "d6934b00-ab23-4aed-bf84-14463e77f6e8"
REPO = "owner/private"
BRANCH = "factory/task/ws"


def task() -> FactoryTask:
    return FactoryTask("Task", REPO, TaskSource("github", REPO, 1), body="Fix it")


def workspace() -> Workspace:
    return Workspace("ws", REPO, BRANCH, "/local/ws")


def execution(path: str = "/workspace/project", profile: str = PROFILE) -> CloudExecution:
    return CloudExecution(path, REPO, profile, "main")


def sandbox(status: str = "RUNNING") -> dict[str, object]:
    return {
        "id": "sbx",
        "status": status,
        "session_api_key": "sandbox-key",
        "exposed_urls": [{"name": "AGENT_SERVER", "url": "https://sandbox.invalid"}],
    }


@dataclass
class BundleProvider:
    fail: bool = False
    revisions: list[RemoteRevision] = field(default_factory=list)
    received: list[bytes] = field(default_factory=list)

    def prepare_input(self, workspace: Workspace) -> tuple[bytes, str]:
        return b"input binary bundle", SHA

    def materialize_bundle(
        self, workspace: Workspace, revision: RemoteRevision, bundle: bytes, base_sha: str
    ) -> None:
        self.revisions.append(revision)
        self.received.append(bundle)
        assert base_sha == SHA
        if self.fail or not bundle:
            raise CloudRevisionError(workspace.workspace_id)


@dataclass
class Files:
    exit_codes: list[int] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    uploads: list[tuple[str, bytes]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    result: bytes = b"result binary bundle"

    def upload(self, path: str, data: bytes) -> None:
        self.uploads.append((path, data))

    def download(self, path: str) -> bytes:
        self.downloads.append(path)
        return self.result

    def bash(self, command: str, *, cwd: str | None = None) -> BashResult:
        self.commands.append(command)
        code = self.exit_codes.pop(0) if self.exit_codes else 0
        return BashResult(code, "untrusted remote output")


def adapter(
    control: FakeTransport,
    conversation: FakeTransport,
    files: Files,
    provider: BundleProvider | None = None,
) -> OpenHandsCloudAdapter:
    return OpenHandsCloudAdapter(
        CloudControlClient("https://cloud.invalid", api_key="cloud-key", transport=control),
        execution(),
        provider or BundleProvider(),
        conversation_transport=conversation,
        files_factory=lambda _sandbox: files,
        sleep=lambda _seconds: None,
        ready_attempts=3,
    )


def dispatched(
    status: str = "idle", *, files: Files | None = None
) -> tuple[AgentRun, FakeTransport, FakeTransport, Files]:
    control = FakeTransport([ServerResponse(200, {"id": "sbx"}), ServerResponse(200, [sandbox()])])
    conversation = FakeTransport([ServerResponse(201, {"id": "conv", "execution_status": status})])
    files = files or Files()
    run = adapter(control, conversation, files).dispatch(task(), workspace())
    return run, control, conversation, files


def collected(
    status: str = "finished", *, files: Files | None = None, provider: BundleProvider | None = None
) -> tuple[AgentRun, Files, BundleProvider]:
    control = FakeTransport([ServerResponse(200, [sandbox()])])
    conversation = FakeTransport(
        [
            ServerResponse(200, {"id": "conv", "execution_status": status}),
            ServerResponse(200, {"commits": [{"sha": RESULT}]}),
            ServerResponse(200, {"response": "done"}),
        ]
    )
    files = files or Files()
    provider = provider or BundleProvider()
    run = AgentRun(task().task_id, AgentKind.OPENHANDS, "conv", RunStatus.RUNNING, workspace())
    run.provider_ref = f"sbx:conv:{SHA}"
    return adapter(control, conversation, files, provider).collect(run), files, provider


def test_secretless_payload_and_binary_preflight() -> None:
    run, control, conversation, files = dispatched()
    assert run.provider_ref == f"sbx:conv:{SHA}"
    assert run.status is RunStatus.PENDING
    assert files.uploads[0][1] == b"input binary bundle"
    assert files.uploads[0][0].startswith("/tmp/factory-input-")
    assert any("bundle" in command and SHA in command for command in files.commands)
    body = json.loads(conversation.last.body or b"{}")
    assert "secrets" not in body
    assert "GITHUB_TOKEN" not in json.dumps(body)
    instruction = body["initial_message"]["content"][0]["text"].lower()
    assert "git_askpass" not in instruction and "clone" not in instruction
    assert "push exactly" not in instruction and "github.com" not in instruction
    assert body["workspace"]["working_dir"] == "/workspace/project"
    assert body["agent_profile_id"] == PROFILE
    assert control.requests[0].headers["Authorization"] == "Bearer cloud-key"
    assert "cloud-key" not in json.dumps(body)


def test_terminal_creation_requires_collection() -> None:
    run, _, _, _ = dispatched("finished")
    assert run.status is RunStatus.PENDING
    assert run.finished_at is None


@pytest.mark.parametrize(
    "path", ["/", "/tmp/project", "/workspace/../etc", "relative", "/workspace//project"]
)
def test_unsafe_working_directory_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match="safe absolute"):
        execution(path)


def test_profile_uuid_is_required() -> None:
    with pytest.raises(ValueError, match="UUID"):
        execution(profile="model-name")


def test_preflight_fails_closed_on_remote_mismatch() -> None:
    files = Files(exit_codes=[1])
    control = FakeTransport([ServerResponse(200, {"id": "sbx"}), ServerResponse(200, [sandbox()])])
    conversation = FakeTransport()
    with pytest.raises(OpenHandsCloudError):
        adapter(control, conversation, files).dispatch(task(), workspace())
    assert not conversation.requests
    assert control.last.method == "DELETE"


def test_collect_transfers_verified_result_bundle() -> None:
    run, files, provider = collected()
    assert run.status is RunStatus.SUCCEEDED
    assert provider.revisions == [RemoteRevision(RESULT, BRANCH, REPO)]
    assert provider.received == [b"result binary bundle"]
    assert len(files.downloads) == 1
    assert any("merge-base --is-ancestor" in command for command in files.commands)


@pytest.mark.parametrize("code", [1, 2, 128])
def test_collect_rejects_wrong_branch_rewritten_base_or_dirty_tree(code: int) -> None:
    run, files, provider = collected(files=Files(exit_codes=[code]))
    assert run.status is RunStatus.FAILED
    assert not files.downloads
    assert not provider.revisions


def test_collect_rejects_missing_or_malformed_bundle() -> None:
    run, _, provider = collected(files=Files(result=b""))
    assert run.status is RunStatus.FAILED
    assert provider.revisions


def test_collect_rejects_git_api_head_disagreeing_with_checkout() -> None:
    run, files, provider = collected(files=Files(exit_codes=[0, 1]))
    assert run.status is RunStatus.FAILED
    assert not files.downloads
    assert not provider.revisions


def test_collect_rejects_mismatched_sha() -> None:
    run, _, _ = collected(provider=BundleProvider(fail=True))
    assert run.status is RunStatus.FAILED


def test_paused_collection_resumes_before_conversation_lookup() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, [sandbox("PAUSED")]),
            ServerResponse(200, {}),
            ServerResponse(200, [sandbox()]),
        ]
    )
    conversation = FakeTransport(
        [ServerResponse(200, {"id": "conv", "execution_status": "running"})]
    )
    run = AgentRun(task().task_id, AgentKind.OPENHANDS, "conv", RunStatus.RUNNING, workspace())
    run.provider_ref = f"sbx:conv:{SHA}"
    assert adapter(control, conversation, Files()).collect(run).status is RunStatus.RUNNING
    assert any(request.url.endswith("/resume") for request in control.requests)


def test_paused_polling_requests_resume_only_once() -> None:
    control = FakeTransport(
        [
            ServerResponse(200, [sandbox("PAUSED")]),
            ServerResponse(200, {}),
            ServerResponse(200, [sandbox("PAUSED")]),
            ServerResponse(200, [sandbox()]),
        ]
    )
    conversation = FakeTransport(
        [ServerResponse(200, {"id": "conv", "execution_status": "running"})]
    )
    run = AgentRun(task().task_id, AgentKind.OPENHANDS, "conv", RunStatus.RUNNING, workspace())
    run.provider_ref = f"sbx:conv:{SHA}"
    assert adapter(control, conversation, Files()).collect(run).status is RunStatus.RUNNING
    assert sum(request.url.endswith("/resume") for request in control.requests) == 1


def test_missing_base_revision_fails_closed() -> None:
    run = AgentRun(task().task_id, AgentKind.OPENHANDS, "conv", RunStatus.RUNNING, workspace())
    run.provider_ref = "sbx:conv"
    with pytest.raises(OpenHandsCloudError):
        adapter(FakeTransport(), FakeTransport(), Files()).collect(run)


def test_provider_ref_parser_accepts_extended_routing_state() -> None:
    assert split_provider_ref(f"sbx:conv:{SHA}", "conv") == ("sbx", "conv")


def test_payload_has_no_secret_fields() -> None:
    body = build_cloud_creation_payload(task(), execution(), workspace()).as_dict()
    assert "secrets" not in body and "secrets_encrypted" not in body


def test_repository_mismatch_prevents_sandbox_creation() -> None:
    control = FakeTransport()
    wrong = Workspace("ws", "other/private", BRANCH, "/local/ws")
    with pytest.raises(OpenHandsCloudError, match="repository identity"):
        adapter(control, FakeTransport(), Files()).dispatch(task(), wrong)
    assert not control.requests


def test_sandbox_lookup_refuses_another_identity() -> None:
    transport = FakeTransport([ServerResponse(200, [sandbox()])])
    control = CloudControlClient(
        "https://cloud.invalid", api_key="private-key", transport=transport
    )
    with pytest.raises(OpenHandsCloudError, match="another sandbox") as caught:
        control.get_sandbox("different")
    assert "private-key" not in repr(caught.value)


def test_provider_ref_rejects_url_metacharacters() -> None:
    with pytest.raises(OpenHandsCloudError, match="invalid provider handle"):
        split_provider_ref(f"sbx&other=1:conv:{SHA}", "conv")


def test_cloud_terminal_release_is_idempotent_and_best_effort() -> None:
    control = FakeTransport([ServerResponse(204), ServerResponse(404)])
    cloud = adapter(control, FakeTransport(), Files())
    run = AgentRun(task().task_id, AgentKind.OPENHANDS, "conv", RunStatus.FAILED, workspace())
    run.provider_ref = f"sbx:conv:{SHA}"
    cloud.release_terminal(run)
    cloud.release_terminal(run)
    assert [request.method for request in control.requests] == ["DELETE", "DELETE"]
