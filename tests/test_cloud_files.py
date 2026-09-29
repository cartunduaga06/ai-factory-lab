"""Agent Server binary transfer checks its documented success response."""

from __future__ import annotations

import io
import urllib.request

import pytest

from factory.integrations.openhands.cloud_files import AgentServerFiles, CloudFileError


@pytest.mark.parametrize("response", [b'{"success":false}', b"{}", b"invalid"])
def test_upload_rejects_unsuccessful_response(
    monkeypatch: pytest.MonkeyPatch, response: bytes
) -> None:
    def open_request(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        assert request.full_url.startswith("https://sandbox.invalid/api/file/upload?path=")
        assert request.get_header("X-session-api-key") == "private-session-key"
        assert timeout == 120.0
        return io.BytesIO(response)

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    client = AgentServerFiles("https://sandbox.invalid", "private-session-key")
    with pytest.raises(CloudFileError, match="upload response invalid") as caught:
        client.upload("/tmp/input.bundle", b"binary\x00data")
    assert "private-session-key" not in repr(caught.value)


def test_upload_accepts_explicit_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def open_request(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        assert b"binary\x00data" in (request.data or b"")
        return io.BytesIO(b'{"success":true,"file_size":11}')

    monkeypatch.setattr(urllib.request, "urlopen", open_request)
    AgentServerFiles("https://sandbox.invalid", "private-session-key").upload(
        "/tmp/input.bundle", b"binary\x00data"
    )


def _bash_response(monkeypatch: pytest.MonkeyPatch, payload: bytes) -> None:
    def open_request(request: urllib.request.Request, *, timeout: float) -> io.BytesIO:
        assert request.full_url.endswith("/api/bash/execute_bash_command")
        return io.BytesIO(payload)

    monkeypatch.setattr(urllib.request, "urlopen", open_request)


def test_bash_normalizes_null_stdout_to_empty_string(monkeypatch: pytest.MonkeyPatch) -> None:
    """A successful command with no output reports ``stdout: null`` on the wire."""
    _bash_response(monkeypatch, b'{"exit_code":0,"stdout":null,"stderr":null}')
    result = AgentServerFiles("https://sandbox.invalid", "private-session-key").bash("true")
    assert result.exit_code == 0
    assert result.stdout == ""


@pytest.mark.parametrize(
    "payload",
    [
        b'{"exit_code":"0","stdout":null}',  # non-integer exit code
        b'{"exit_code":0,"stdout":123}',  # non-string, non-null stdout
        b'{"stdout":"done"}',  # missing exit code
        b"invalid",  # not JSON
    ],
)
def test_bash_still_fails_closed_on_invalid_payloads(
    monkeypatch: pytest.MonkeyPatch, payload: bytes
) -> None:
    _bash_response(monkeypatch, payload)
    with pytest.raises(CloudFileError, match="command response invalid"):
        AgentServerFiles("https://sandbox.invalid", "private-session-key").bash("true")
