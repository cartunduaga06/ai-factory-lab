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
