"""Supported Agent Server binary file and bash APIs for Cloud workspaces."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid4


class CloudFileError(RuntimeError):
    """A sandbox file or command operation failed; remote output is discarded."""


MAX_RESPONSE_BYTES = 200 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class BashResult:
    exit_code: int
    stdout: str


class CloudFileClient(Protocol):
    def upload(self, path: str, data: bytes) -> None: ...

    def download(self, path: str) -> bytes: ...

    def bash(self, command: str, *, cwd: str | None = None) -> BashResult: ...


class AgentServerFiles:
    """Binary transfer stays outside the JSON conversation transport."""

    def __init__(self, url: str, session_key: str | None, *, timeout: float = 120.0) -> None:
        self._url = url.rstrip("/")
        self._key = session_key
        self._timeout = timeout

    def _request(self, path: str, data: bytes | None, content_type: str | None = None) -> bytes:
        headers: dict[str, str] = {}
        if self._key:
            headers["X-Session-API-Key"] = self._key
        if content_type:
            headers["Content-Type"] = content_type
        request = urllib.request.Request(self._url + path, data=data, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                result: bytes = response.read(MAX_RESPONSE_BYTES + 1)
                if len(result) > MAX_RESPONSE_BYTES:
                    raise CloudFileError("sandbox response exceeds transfer limit")
                return result
        except (urllib.error.URLError, OSError):
            raise CloudFileError("sandbox file operation failed") from None

    def upload(self, path: str, data: bytes) -> None:
        boundary = uuid4().hex
        body = (
            (
                f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
                f'filename="bundle"\r\nContent-Type: application/octet-stream\r\n\r\n'
            ).encode()
            + data
            + f"\r\n--{boundary}--\r\n".encode()
        )
        response = self._request(
            "/api/file/upload?path=" + urllib.parse.quote(path, safe=""),
            body,
            f"multipart/form-data; boundary={boundary}",
        )
        try:
            result = json.loads(response)
            if not isinstance(result, dict) or result.get("success") is not True:
                raise ValueError
        except (ValueError, TypeError):
            raise CloudFileError("sandbox upload response invalid") from None

    def download(self, path: str) -> bytes:
        return self._request("/api/file/download?path=" + urllib.parse.quote(path, safe=""), None)

    def bash(self, command: str, *, cwd: str | None = None) -> BashResult:
        payload: dict[str, object] = {"command": command, "timeout": int(self._timeout)}
        if cwd is not None:
            payload["cwd"] = cwd
        body = self._request(
            "/api/bash/execute_bash_command", json.dumps(payload).encode(), "application/json"
        )
        try:
            parsed = json.loads(body)
            code = parsed["exit_code"]
            stdout = parsed["stdout"]
            if not isinstance(code, int) or not isinstance(stdout, str):
                raise ValueError
            return BashResult(code, stdout)
        except (ValueError, KeyError, TypeError):
            raise CloudFileError("sandbox command response invalid") from None


__all__ = ["AgentServerFiles", "BashResult", "CloudFileClient", "CloudFileError"]
