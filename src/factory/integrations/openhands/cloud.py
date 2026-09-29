"""OpenHands Cloud as a factory :class:`~factory.domain.models.AgentAdapter`.

This is the *cloud* counterpart of :mod:`factory.integrations.openhands.adapter`.
It drives the same OpenHands conversation contract, but against a sandbox that
OpenHands Cloud provisions on the factory's behalf. It is selected explicitly
through ``OPENHANDS_BACKEND=cloud`` and never becomes the default.

Contract discovery
------------------

The Cloud control API manages sandbox creation, lookup and resume. The supported
Agent Server bash and binary file APIs transfer Git bundles before and after the
conversation. The local worktree's base SHA is persisted with the provider handle
and remains the authority; the sandbox receives no GitHub credential. Terminal
success is accepted only after a locally verified bundle import.

Safety
------

* The HTTP transports are injectable; tests never touch the network.
* Nothing raw escapes: the API key lives in a private attribute and never appears
  in a URL, ``repr``, log line or exception. Remote error bodies are discarded and
  only trusted local data (the numeric HTTP status, the sandbox/conversation
  identifiers) is attached to an error.
* The conversation id *is* the factory ``run_id``; the sandbox id and conversation
  id are persisted together as ``AgentRun.provider_ref`` so re-collection after a
  restart is idempotent and never creates a second sandbox or conversation.
"""

from __future__ import annotations

import contextlib
import re
import shlex
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath
from uuid import UUID, uuid4

from factory.domain.enums import AgentKind, RunStatus
from factory.domain.models import AgentRun, FactoryTask, RemoteRevision, Workspace
from factory.integrations.base import AgentAdapterBase
from factory.integrations.openhands.client import (
    MAX_DETAIL_CHARS,
    OpenHandsClient,
    OpenHandsError,
    ServerResponse,
    Transport,
    UrllibTransport,
    _as_mapping,
    bound,
    mask_url,
    redact,
)
from factory.integrations.openhands.cloud_files import AgentServerFiles, CloudFileClient
from factory.integrations.openhands.execution import (
    DEFAULT_MAX_ITERATIONS,
    build_instruction,
)
from factory.integrations.openhands.status import is_terminal, map_status
from factory.integrations.workspace.cloud_bundle import CloudBundleProvider

#: Cloud control API paths (versioned, as exposed by the Cloud platform).
SANDBOXES_PATH = "/api/v1/sandboxes"
AGENT_SERVER_URL_NAME = "AGENT_SERVER"

#: Bound on how long the factory waits for a fresh sandbox to become RUNNING.
DEFAULT_SANDBOX_READY_ATTEMPTS = 60
DEFAULT_SANDBOX_POLL_SECONDS = 5.0

#: Separator between the sandbox id and the conversation id in ``provider_ref``.
#: Both are opaque identifiers that cannot contain a colon.
PROVIDER_REF_SEPARATOR = ":"

#: A stable sandbox directory used as the working directory for commands that
#: delete or replace the repository checkout. It is never removed by the factory,
#: so a bash session can always resolve its current working directory.
SAFE_BASH_CWD = "/"
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]+\Z")


class OpenHandsCloudError(OpenHandsError):
    """Base class for every OpenHands Cloud integration failure.

    Sanitized like every other integration error: never carries a token, a
    request header, a raw response body or a credential-bearing URL.
    """


class OpenHandsCloudConfigurationError(OpenHandsCloudError):
    """The Cloud backend was selected with unusable configuration."""


class OpenHandsCloudRequestError(OpenHandsCloudError):
    """The Cloud control API answered with an unexpected HTTP status.

    Built from trusted local data only (the numeric status); remote error-body
    text is discarded at the client boundary.
    """

    def __init__(self, status: int) -> None:
        super().__init__(f"OpenHands Cloud returned status {status}")
        self.status = status


class OpenHandsCloudResponseError(OpenHandsCloudError):
    """The Cloud control API returned a body the client could not interpret."""


class OpenHandsCloudRevisionError(OpenHandsCloudError):
    """A Cloud run produced no independently retrievable, safe revision.

    The run is *failed closed*: the factory never publishes an unverifiable
    cloud result, and this error is raised only after the run has been marked
    terminal ``FAILED``. The message names the run only.
    """

    def __init__(self, run_id: str) -> None:
        super().__init__(f"OpenHands Cloud run {run_id} has no verifiable revision")
        self.run_id = run_id


@dataclass(slots=True, frozen=True)
class CloudSandbox:
    """A provisioned Cloud runtime, reduced to what the adapter needs."""

    sandbox_id: str
    session_api_key: str | None
    status: str
    agent_server_url: str | None


def split_provider_ref(provider_ref: str | None, run_id: str) -> tuple[str, str]:
    """Decode ``AgentRun.provider_ref`` into ``(sandbox_id, conversation_id)``.

    Raises:
        OpenHandsCloudError: if the handle is missing or malformed. A run whose
            Cloud routing state is unusable must fail closed rather than create a
            second sandbox.
    """
    if not provider_ref:
        raise OpenHandsCloudError(f"OpenHands Cloud run {run_id} has no provider handle")
    sandbox_id, separator, remainder = provider_ref.partition(PROVIDER_REF_SEPARATOR)
    conversation_id = remainder.partition(PROVIDER_REF_SEPARATOR)[0]
    if (
        not separator
        or not _IDENTIFIER.fullmatch(sandbox_id)
        or not _IDENTIFIER.fullmatch(conversation_id)
    ):
        raise OpenHandsCloudError(f"OpenHands Cloud run {run_id} has an invalid provider handle")
    return sandbox_id, conversation_id


_SHA = re.compile(r"[0-9a-f]{40,64}\Z")
_BRANCH = re.compile(r"factory/[A-Za-z0-9_./-]+\Z")


def _split_revision_ref(provider_ref: str | None, run_id: str) -> tuple[str, str, str]:
    sandbox, conversation = split_provider_ref(provider_ref, run_id)
    parts = (provider_ref or "").split(":")
    if len(parts) != 3 or not _SHA.fullmatch(parts[2]):
        raise OpenHandsCloudError(f"OpenHands Cloud run {run_id} has no base revision")
    return sandbox, conversation, parts[2]


def _entry_url(exposed_urls: object, name: str) -> str | None:
    if not isinstance(exposed_urls, Sequence):
        return None
    for entry in exposed_urls:
        mapping = _as_mapping(entry)
        if mapping is not None and mapping.get("name") == name:
            url = mapping.get("url")
            if isinstance(url, str) and url.strip():
                return url.strip()
    return None


class CloudControlClient:
    """Small client for the OpenHands Cloud control API (sandbox lifecycle)."""

    def __init__(
        self,
        api_url: str,
        *,
        api_key: str,
        transport: Transport | None = None,
        timeout: float = 60.0,
    ) -> None:
        cleaned = api_url.strip().rstrip("/")
        if not cleaned:
            raise OpenHandsCloudConfigurationError("OpenHands Cloud API URL must not be empty")
        if not api_key:
            raise OpenHandsCloudConfigurationError("OpenHands Cloud API key must not be empty")
        self._api_url = cleaned
        self._api_key = api_key
        self._transport = transport or UrllibTransport(timeout)

    def __repr__(self) -> str:
        # The API key is never rendered.
        return f"CloudControlClient(api_url={mask_url(self._api_url)!r})"

    @property
    def api_url(self) -> str:
        return mask_url(self._api_url)

    def secret_values(self) -> tuple[str, ...]:
        return (self._api_key,)

    def create_sandbox(self, sandbox_spec_id: str | None = None) -> CloudSandbox:
        """Create a Cloud runtime and return its initial descriptor."""
        path = SANDBOXES_PATH
        if sandbox_spec_id:
            path = f"{path}?sandbox_spec_id={sandbox_spec_id}"
        body = self._request("POST", path)
        sandbox_id = body.get("id")
        if not isinstance(sandbox_id, str) or not _IDENTIFIER.fullmatch(sandbox_id):
            raise OpenHandsCloudResponseError("sandbox creation returned no id")
        session_key = body.get("session_api_key")
        return CloudSandbox(
            sandbox_id=sandbox_id,
            session_api_key=session_key if isinstance(session_key, str) else None,
            status="STARTING",
            agent_server_url=None,
        )

    def get_sandbox(self, sandbox_id: str) -> CloudSandbox | None:
        """Return the runtime descriptor, or ``None`` if it no longer exists."""
        response = self._send("GET", SANDBOXES_PATH, params={"id": sandbox_id})
        if response.status == 404:
            return None
        self._raise_for_status(response)
        if not isinstance(response.body, list) or not response.body:
            raise OpenHandsCloudResponseError("sandbox lookup returned no list")
        entry = _as_mapping(response.body[0])
        if entry is None:
            raise OpenHandsCloudResponseError("sandbox lookup returned no object")
        if entry.get("id") != sandbox_id:
            raise OpenHandsCloudResponseError("sandbox lookup returned another sandbox")
        status = entry.get("status")
        session_key = entry.get("session_api_key")
        return CloudSandbox(
            sandbox_id=sandbox_id,
            session_api_key=session_key if isinstance(session_key, str) else None,
            status=status if isinstance(status, str) else "UNKNOWN",
            agent_server_url=_entry_url(entry.get("exposed_urls"), AGENT_SERVER_URL_NAME),
        )

    def resume_sandbox(self, sandbox_id: str) -> None:
        """Resume a paused runtime."""
        self._request("POST", f"{SANDBOXES_PATH}/{sandbox_id}/resume")

    # -- internals ---------------------------------------------------------

    def _request(self, method: str, path: str) -> Mapping[str, object]:
        response = self._send(method, path)
        self._raise_for_status(response)
        body = _as_mapping(response.body)
        if body is None:
            raise OpenHandsCloudResponseError("Cloud API returned no JSON object")
        return body

    def _send(
        self, method: str, path: str, *, params: Mapping[str, str] | None = None
    ) -> ServerResponse:
        url = f"{self._api_url}{path}"
        if params:
            url = f"{url}?" + "&".join(f"{key}={value}" for key, value in params.items())
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._api_key}"}
        return self._transport.send(method, url, headers, None)

    def _raise_for_status(self, response: ServerResponse) -> None:
        if response.ok:
            return
        # Only the trusted numeric status crosses the boundary.
        raise OpenHandsCloudRequestError(response.status)


@dataclass(slots=True, frozen=True)
class CloudExecution:
    """Configuration for a Cloud-dispatched conversation.

    Mirrors :class:`~factory.integrations.openhands.execution.OpenHandsExecution`
    where the agent-server contract is identical, and adds the Cloud-specific
    working directory and repository identity. The *branch* is not configured: it
    is the per-run :attr:`~factory.domain.models.Workspace.branch` the factory
    assigns, exactly as in local mode, so a retry still gets its own branch.
    """

    working_dir: str
    repository: str
    profile: str
    base_ref: str
    sandbox_spec_id: str | None = None
    max_iterations: int = DEFAULT_MAX_ITERATIONS
    stuck_detection: bool = True
    autotitle: bool = True

    def __post_init__(self) -> None:
        path = PurePosixPath(self.working_dir)
        if (
            not self.working_dir.startswith("/workspace/")
            or str(path) != self.working_dir
            or ".." in path.parts
            or "." in path.parts
            or len(path.parts) != 3
            or any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in path.parts[1:])
        ):
            raise ValueError("cloud working_dir must be a safe absolute sandbox path")
        if not self.repository.strip() or "/" not in self.repository:
            raise ValueError("cloud repository must be 'owner/name'")
        try:
            UUID(self.profile)
        except ValueError:
            raise ValueError("cloud profile must be an Agent Profile UUID") from None
        if not self.base_ref.strip() or self.base_ref.startswith("-"):
            raise ValueError("cloud base_ref must be a safe non-empty ref")
        if self.max_iterations < 1:
            raise ValueError("max_iterations must be positive")


@dataclass(slots=True, frozen=True)
class CloudConversationPayload:
    """A fully-formed Cloud conversation-creation request."""

    body: dict[str, object] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return dict(self.body)


def build_cloud_creation_payload(
    task: FactoryTask,
    execution: CloudExecution,
    workspace: Workspace,
) -> CloudConversationPayload:
    """Build only supported conversation fields, with no secrets or Git network workflow."""
    body: dict[str, object] = {
        "workspace": {"kind": "LocalWorkspace", "working_dir": execution.working_dir},
        "confirmation_policy": {"kind": "NeverConfirm"},
        "max_iterations": execution.max_iterations,
        "stuck_detection": execution.stuck_detection,
        "autotitle": execution.autotitle,
        "initial_message": {
            "role": "user",
            "content": [
                {"type": "text", "text": build_cloud_instruction(task, execution, workspace)}
            ],
            "run": True,
        },
        "agent_profile_id": execution.profile,
    }
    return CloudConversationPayload(body=body)


def build_cloud_instruction(
    task: FactoryTask,
    execution: CloudExecution,
    workspace: Workspace,
) -> str:
    """Describe the factory-prepared checkout without credentials."""
    return (
        f"{build_instruction(task)}\n\n"
        f"Work only in `{execution.working_dir}` on branch `{workspace.branch}`. "
        "The factory prepared the repository. Commit intended changes on this "
        "branch. Do not change branches or rewrite history. The factory "
        "transfers and publishes the result.\n"
    )


class OpenHandsCloudAdapter(AgentAdapterBase):
    """Drives OpenHands Cloud sandbox conversations as factory agent runs.

    The adapter owns the cloud sandbox lifecycle for a run and defers to the
    existing Agent Server client for conversation create/collect/cancel, so the
    status mapping stays a single source of truth (:mod:`factory.integrations
    .openhands.status`).

    A terminal success is accepted only after :meth:`CloudRevisionProvider`
    resolves and materialises the exact cloud revision locally. If it cannot, the
    adapter returns the run as ``FAILED`` — the factory then follows the existing
    failure path and never publishes, which is the documented fail-closed
    outcome.
    """

    def __init__(
        self,
        control: CloudControlClient,
        execution: CloudExecution,
        revision_provider: CloudBundleProvider,
        *,
        conversation_transport: Transport | None = None,
        files_factory: Callable[[CloudSandbox], CloudFileClient] | None = None,
        poll_interval: float = DEFAULT_SANDBOX_POLL_SECONDS,
        ready_attempts: int = DEFAULT_SANDBOX_READY_ATTEMPTS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._control = control
        self._execution = execution
        self._revision_provider = revision_provider
        self._conversation_transport = conversation_transport
        self._files_factory = files_factory or self._default_files
        self._poll_interval = max(0.0, poll_interval)
        self._ready_attempts = max(1, ready_attempts)
        self._sleep = sleep

    def __repr__(self) -> str:
        # Both clients have credential-free reprs.
        return f"OpenHandsCloudAdapter(control={self._control!r})"

    @property
    def kind(self) -> AgentKind:
        return AgentKind.OPENHANDS

    # -- dispatch ----------------------------------------------------------

    def dispatch(self, task: FactoryTask, workspace: Workspace) -> AgentRun:
        """Provision a sandbox and start the conversation for ``task``.

        Raises:
            OpenHandsCloudError: a sanitized integration error if the sandbox or
                conversation could not be created. The sandbox is best-effort
                released before the error is raised.
        """
        if (
            task.target_repository != self._execution.repository
            or workspace.repository_slug != self._execution.repository
        ):
            raise OpenHandsCloudConfigurationError("Cloud repository identity mismatch")
        # TODO: sandbox creation precedes durable run persistence. Recovering a
        # crash in this window needs a transactional lifecycle/outbox design.
        sandbox = self._control.create_sandbox(self._execution.sandbox_spec_id)
        try:
            ready = self._await_ready(sandbox)
            client = self._conversation_client(ready)
            bundle, base_sha = self._revision_provider.prepare_input(workspace)
            self._prepare_remote(self._files_factory(ready), workspace, bundle, base_sha)
            payload = build_cloud_creation_payload(
                task,
                self._execution,
                workspace,
            ).as_dict()
            descriptor = client.create_conversation(payload)
            conversation_id = _conversation_id(descriptor)
            status = map_status(descriptor.get("execution_status"))
            if is_terminal(status):
                status = RunStatus.PENDING
        except Exception:
            # Best-effort cleanup; the original sanitized error is re-raised.
            self._release(sandbox.sandbox_id)
            raise
        provider_ref = f"{ready.sandbox_id}:{conversation_id}:{base_sha}"
        return AgentRun(
            task_id=task.task_id,
            adapter=self.kind,
            run_id=conversation_id,
            status=status,
            workspace=workspace,
            provider_ref=provider_ref,
            started_at=datetime.now(UTC),
            finished_at=datetime.now(UTC) if is_terminal(status) else None,
        )

    # -- collect -----------------------------------------------------------

    def collect(self, run: AgentRun) -> AgentRun:
        """Refresh ``run`` from its sandbox conversation, idempotently.

        Re-collection re-derives the sandbox and conversation from the persisted
        ``provider_ref`` and never provisions a second sandbox. On a terminal
        success the exact cloud revision is resolved and materialised into the
        run's local validation workspace; if that is impossible, the run is
        failed closed.

        Raises:
            OpenHandsCloudError: if the run cannot be located or queried, or its
                provider handle is missing/malformed.
        """
        sandbox_id, conversation_id, base_sha = _split_revision_ref(run.provider_ref, run.run_id)
        sandbox = self._await_ready(CloudSandbox(sandbox_id, None, "UNKNOWN", None))
        if sandbox.agent_server_url is None:
            raise OpenHandsCloudError(
                f"OpenHands Cloud sandbox for run {run.run_id} could not be located"
            )

        client = self._conversation_client(sandbox)
        descriptor = client.get_conversation(conversation_id)
        if descriptor is None:
            raise OpenHandsCloudError(
                f"OpenHands Cloud conversation {conversation_id} could not be located"
            )

        status = map_status(descriptor.get("execution_status"))
        if status is RunStatus.SUCCEEDED:
            status = self._materialize_or_fail(run, client, self._files_factory(sandbox), base_sha)
        run.status = status
        if is_terminal(status):
            run.finished_at = run.finished_at or datetime.now(UTC)
            run.summary = self._final_summary(client, conversation_id)
        else:
            run.finished_at = None
        return run

    # -- cancel ------------------------------------------------------------

    def cancel(self, run: AgentRun) -> None:
        """Interrupt the conversation and release the sandbox. Idempotent.

        A run that is already terminal in the factory's own view is left alone.
        A missing sandbox or conversation is treated as already cancelled.
        """
        if run.is_terminal:
            return
        sandbox_id, conversation_id = split_provider_ref(run.provider_ref, run.run_id)
        sandbox = self._control.get_sandbox(sandbox_id)
        if sandbox is not None and sandbox.agent_server_url is not None:
            try:
                client = self._conversation_client(sandbox)
                client.interrupt_conversation(conversation_id)
            except OpenHandsError:
                # The conversation may already be gone; the sandbox is still
                # released below, so cancellation remains idempotent.
                pass
        self._release(sandbox_id)

    # -- internals ---------------------------------------------------------

    def _await_ready(self, sandbox: CloudSandbox) -> CloudSandbox:
        """Poll the sandbox until it is ``RUNNING`` with an agent-server URL."""
        current = sandbox
        resume_requested = False
        for _ in range(self._ready_attempts):
            fetched = self._control.get_sandbox(current.sandbox_id)
            if fetched is None:
                raise OpenHandsCloudError(f"OpenHands Cloud sandbox {current.sandbox_id} vanished")
            if fetched.status == "RUNNING" and fetched.agent_server_url:
                return fetched
            if fetched.status == "PAUSED":
                if not resume_requested:
                    self._control.resume_sandbox(fetched.sandbox_id)
                    resume_requested = True
                current = fetched
                self._sleep(self._poll_interval)
                continue
            if fetched.status in {"ERROR", "MISSING"}:
                raise OpenHandsCloudError(
                    f"OpenHands Cloud sandbox {current.sandbox_id} failed to start"
                )
            current = fetched
            self._sleep(self._poll_interval)
        raise OpenHandsCloudError(
            f"OpenHands Cloud sandbox {current.sandbox_id} did not become ready"
        )

    def _materialize_or_fail(
        self, run: AgentRun, client: OpenHandsClient, files: CloudFileClient, base_sha: str
    ) -> RunStatus:
        workspace = run.workspace
        if workspace is None:
            return RunStatus.FAILED
        try:
            self._verify_remote(files, workspace, base_sha)
            commit_sha = client.head_commit(self._execution.working_dir)
            if commit_sha is None or not _SHA.fullmatch(commit_sha):
                return RunStatus.FAILED
            directory = shlex.quote(self._execution.working_dir)
            self._require_bash(
                files,
                f'test "$(git -C {directory} rev-parse HEAD)" = {shlex.quote(commit_sha)}',
            )
            result_path = f"/tmp/factory-result-{uuid4().hex}.bundle"
            try:
                self._require_bash(
                    files,
                    f"git -C {shlex.quote(self._execution.working_dir)} bundle create "
                    f"{shlex.quote(result_path)} {shlex.quote('refs/heads/' + workspace.branch)}",
                )
                bundle = files.download(result_path)
            finally:
                self._remove_temp(files, result_path)
            revision = RemoteRevision(commit_sha, workspace.branch, workspace.repository_slug)
            self._revision_provider.materialize_bundle(workspace, revision, bundle, base_sha)
        except Exception:  # noqa: BLE001 - discard untrusted remote output
            return RunStatus.FAILED
        return RunStatus.SUCCEEDED

    @staticmethod
    def _default_files(sandbox: CloudSandbox) -> CloudFileClient:
        if sandbox.agent_server_url is None:
            raise OpenHandsCloudError("sandbox has no agent server URL")
        return AgentServerFiles(sandbox.agent_server_url, sandbox.session_api_key)

    @staticmethod
    def _require_bash(files: CloudFileClient, command: str, *, cwd: str | None = None) -> str:
        result = files.bash(command, cwd=cwd)
        if result.exit_code != 0:
            raise OpenHandsCloudError("sandbox Git verification failed")
        return result.stdout.strip()

    @staticmethod
    def _remove_temp(files: CloudFileClient, path: str) -> None:
        with contextlib.suppress(Exception):
            files.bash(f"rm -f -- {shlex.quote(path)}")

    def _prepare_remote(
        self, files: CloudFileClient, workspace: Workspace, bundle: bytes, base_sha: str
    ) -> None:
        directory = self._execution.working_dir
        if not _BRANCH.fullmatch(workspace.branch) or not _SHA.fullmatch(base_sha):
            raise OpenHandsCloudError("unsafe Cloud branch or base revision")
        input_path = f"/tmp/factory-input-{uuid4().hex}.bundle"
        files.upload(input_path, bundle)
        try:
            quoted_dir = shlex.quote(directory)
            quoted_branch = shlex.quote(workspace.branch)
            quoted_bundle = shlex.quote(input_path)
            parent = shlex.quote(str(PurePosixPath(directory).parent))
            command = (
                f"test ! -L {parent} && mkdir -p -- {parent} && "
                f"rm -rf -- {quoted_dir} && "
                f"git clone --no-checkout --branch {quoted_branch} {quoted_bundle} {quoted_dir} && "
                f"git -C {quoted_dir} switch {quoted_branch} && "
                f"git -C {quoted_dir} remote remove origin && "
                f'test "$(git -C {quoted_dir} rev-parse HEAD)" = {shlex.quote(base_sha)} && '
                f'test "$(git -C {quoted_dir} rev-parse --abbrev-ref HEAD)" = {quoted_branch}'
            )
            # The bash session persists between calls and may already have the
            # target directory as its current working directory. Removing that
            # directory from within it leaves the shell unable to resolve its CWD,
            # so the destructive preparation runs from a stable directory the
            # command never deletes.
            self._require_bash(files, command, cwd=SAFE_BASH_CWD)
        finally:
            self._remove_temp(files, input_path)

    def _verify_remote(self, files: CloudFileClient, workspace: Workspace, base_sha: str) -> None:
        directory = shlex.quote(self._execution.working_dir)
        branch = shlex.quote(workspace.branch)
        sha = shlex.quote(base_sha)
        self._require_bash(
            files,
            f'test "$(git -C {directory} rev-parse --abbrev-ref HEAD)" = {branch} && '
            f"git -C {directory} merge-base --is-ancestor {sha} HEAD && "
            f'test -z "$(git -C {directory} status --porcelain --untracked-files=all)"',
        )

    def release_terminal(self, run: AgentRun) -> None:
        """Release after tracking has persisted and reconciled terminal state."""
        if run.is_terminal and run.provider_ref:
            try:
                sandbox_id, _ = split_provider_ref(run.provider_ref, run.run_id)
                self._release(sandbox_id)
            except Exception:  # noqa: BLE001 - idempotent best effort cleanup
                pass

    def _conversation_client(self, sandbox: CloudSandbox) -> OpenHandsClient:
        if sandbox.agent_server_url is None:
            raise OpenHandsCloudError("OpenHands Cloud sandbox has no agent server URL")
        return OpenHandsClient(
            sandbox.agent_server_url,
            session_api_key=sandbox.session_api_key,
            transport=self._conversation_transport or UrllibTransport(),
        )

    def _release(self, sandbox_id: str) -> None:
        try:
            self._control._send("DELETE", f"{SANDBOXES_PATH}/{sandbox_id}")
        except Exception:  # noqa: BLE001 - release is best-effort
            return

    def _final_summary(self, client: OpenHandsClient, conversation_id: str) -> str | None:
        text = client.agent_final_response(conversation_id)
        if text is None:
            return None
        secrets = (*self._control.secret_values(), *client.secret_values())
        return bound(redact(text, secrets), MAX_DETAIL_CHARS)


def _conversation_id(descriptor: object) -> str:
    mapping = _as_mapping(descriptor)
    if mapping is None:
        raise OpenHandsCloudResponseError("conversation creation returned no descriptor")
    value = mapping.get("id")
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise OpenHandsCloudResponseError("conversation creation returned no id")
    return value


__all__ = [
    "AGENT_SERVER_URL_NAME",
    "DEFAULT_SANDBOX_POLL_SECONDS",
    "DEFAULT_SANDBOX_READY_ATTEMPTS",
    "PROVIDER_REF_SEPARATOR",
    "SAFE_BASH_CWD",
    "SANDBOXES_PATH",
    "CloudControlClient",
    "CloudConversationPayload",
    "CloudExecution",
    "CloudSandbox",
    "OpenHandsCloudAdapter",
    "OpenHandsCloudConfigurationError",
    "OpenHandsCloudError",
    "OpenHandsCloudRequestError",
    "OpenHandsCloudResponseError",
    "OpenHandsCloudRevisionError",
    "build_cloud_creation_payload",
    "build_cloud_instruction",
    "split_provider_ref",
]
