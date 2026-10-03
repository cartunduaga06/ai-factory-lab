"""Sanitization and fail-closed behavior for host health observations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from factory.integrations.host_health import HostHealthCollector


def test_host_metrics_timestamp_and_missing_evidence(tmp_path: Path) -> None:
    proc = tmp_path / "proc"
    (proc / "pressure").mkdir(parents=True)
    (proc / "meminfo").write_text(
        "MemTotal: 1000 kB\nMemAvailable: 100 kB\nSwapTotal: 0 kB\nSwapFree: 0 kB\n"
    )
    (proc / "loadavg").write_text("3.5 2.0 1.0 1/2 3\n")
    (proc / "uptime").write_text("12.5 4.0\n")
    (proc / "pressure" / "memory").write_text("some avg10=30.00 avg60=20 avg300=10 total=1\n")
    report = HostHealthCollector(
        root=tmp_path,
        proc=proc,
        wall_clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    ).collect()
    assert report["timestamp"] == "2026-01-01T00:00:00+00:00"
    metrics = report["metrics"]
    assert isinstance(metrics, dict)
    assert metrics["cpu_load_1m"]["status"] == "CRITICAL"
    assert metrics["memory_used_pct"]["value"] == 90
    assert metrics["memory_pressure_some_10s"]["status"] == "CRITICAL"
    assert metrics["swap_used_pct"]["status"] == "UNKNOWN"


def test_worker_attribution_is_allowlisted_and_sanitized(tmp_path: Path) -> None:
    report = HostHealthCollector(
        root=tmp_path,
        proc=tmp_path / "missing",
        workers=lambda: [
            {
                "pid": 42,
                "cpu_pct": 5,
                "memory_bytes": 99,
                "project_id": "ai-factory-lab",
                "task_id": "task-1",
                "cmdline": "token=secret",
            },
            {"pid": "bad", "cmdline": "ghp_secret"},
        ],
    ).collect()
    encoded = json.dumps(report)
    assert "token=secret" not in encoded and "ghp_secret" not in encoded
    assert report["workers"] == [
        {
            "pid": 42,
            "cpu_pct": 5,
            "memory_bytes": 99,
            "project_id": "ai-factory-lab",
            "task_id": "task-1",
        }
    ]
