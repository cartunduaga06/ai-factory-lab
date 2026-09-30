"""Small read-only HTTP view for an operator's trusted local network or proxy."""

from __future__ import annotations

import html
import json
import threading
from collections.abc import Callable
from contextlib import suppress
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from factory.domain.models import StatusSnapshot
from factory.orchestration.status import StatusService


def serve_status(
    service: StatusService,
    host: str = "127.0.0.1",
    port: int = 8765,
    pulse: Callable[[], None] | None = None,
) -> None:
    """Serve GET /factory/status; the server offers no write method."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - stdlib handler contract
            parsed = urlsplit(self.path)
            if parsed.path != "/factory/status":
                self.send_error(404)
                return
            task_id = parse_qs(parsed.query).get("task_id", [None])[0]
            try:
                snapshot = service.for_task(task_id) if task_id else service.current()
            except KeyError:
                self.send_error(404)
                return
            if "application/json" in self.headers.get("Accept", ""):
                body = json.dumps(asdict(snapshot)).encode("utf-8")
                content_type = "application/json; charset=utf-8"
            else:
                body = render_status(snapshot).encode("utf-8")
                content_type = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            # Request paths and query strings can contain sensitive values.
            pass

    stop = threading.Event()
    if pulse is not None:
        callback = pulse

        def monitor() -> None:
            while not stop.is_set():
                with suppress(Exception):  # durable outbox retries on next pulse
                    callback()
                stop.wait(60)

        threading.Thread(target=monitor, daemon=True).start()
    try:
        with ThreadingHTTPServer((host, port), Handler) as server:
            server.serve_forever()
    finally:
        stop.set()


def render_status(snapshot: StatusSnapshot) -> str:
    """Render a single-column phone view with all dynamic content escaped."""
    escape = html.escape
    rows = (
        ("Task", snapshot.task_id),
        ("Agent", snapshot.agent),
        ("Run", snapshot.run_id),
        ("Workspace", snapshot.workspace_id),
        ("Branch", snapshot.branch),
        ("Started", snapshot.started_at),
        ("Heartbeat", snapshot.last_heartbeat),
        ("Last transition", snapshot.last_transition),
        ("Finished", snapshot.finished_at),
        ("Evidence", snapshot.evidence),
        ("Action", snapshot.action),
    )
    details = "".join(
        f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>"
        for label, value in rows
        if value is not None
    )
    links = "".join(
        f'<a href="{escape(url, quote=True)}" rel="noopener noreferrer">{label}</a>'
        for label, url in (("Issue", snapshot.issue_url), ("Pull request", snapshot.pr_url))
        if url is not None
    )
    history = "".join(f"<li>{escape(item)}</li>" for item in snapshot.history)
    gates = "".join(f"<li>{escape(item)}</li>" for item in snapshot.gates)
    previous_gates = "".join(f"<li>{escape(item)}</li>" for item in snapshot.previous_gates)
    return (
        '<!doctype html><html lang="en"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Factory status</title><style>"
        "body{font:16px system-ui;background:#101820;color:#f2f5f7;margin:0;padding:1rem}"
        "main{max-width:36rem;margin:auto}h1{font-size:1.3rem}strong{font-size:2rem}"
        "dt{color:#a7becb;margin-top:1rem}dd{margin:.2rem 0;overflow-wrap:anywhere}"
        "a{display:inline-block;color:#8edaff;margin:1rem 1rem 0 0}li{margin:.5rem 0}"
        "</style><main><h1>AI Factory Lab</h1>"
        f"<strong>{escape(snapshot.phase)}</strong><dl>{details}</dl>{links}"
        f"<h2>Quality gates</h2><ul>{gates}</ul>"
        f"<h2>Previous gate results</h2><ul>{previous_gates}</ul>"
        f"<h2>Recent transitions</h2><ol>{history}</ol></main></html>"
    )
