"""Read-only, bounded host metrics and worker attribution collector."""

from __future__ import annotations

import json
import os
import shutil
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path

from factory.domain.ports import DockerInspector, ServiceHealthChecker


def _read_number(path: Path, key: str | None = None) -> float | None:
    try:
        for line in path.read_text(encoding="ascii").splitlines():
            parts = line.split()
            if key is None or (parts and parts[0].rstrip(":") == key):
                value = float(parts[1] if key else parts[0])
                return value if value >= 0 else None
    except (OSError, ValueError, IndexError):
        return None
    return None


def _health(value: float | None, warning: float, critical: float) -> str:
    if value is None:
        return "UNKNOWN"
    return "CRITICAL" if value >= critical else "WARNING" if value >= warning else "HEALTHY"


class HostHealthCollector:
    """Collect allowlisted Linux metrics and explicitly registered health probes."""

    def __init__(
        self,
        *,
        root: Path = Path("/"),
        proc: Path = Path("/proc"),
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        service_checker: ServiceHealthChecker | None = None,
        service_targets: tuple[str, ...] = (),
        docker_inspector: DockerInspector | None = None,
        docker_targets: tuple[str, ...] = (),
        workers: Callable[[], list[Mapping[str, object]]] | None = None,
    ) -> None:
        self._root, self._proc = root, proc
        self._monotonic, self._wall_clock = monotonic, wall_clock
        self._service_checker, self._service_targets = service_checker, service_targets
        self._docker_inspector, self._docker_targets = docker_inspector, docker_targets
        self._workers = workers

    def collect(self) -> dict[str, object]:
        stamp = self._wall_clock().astimezone(UTC).isoformat()
        mem_total = _read_number(self._proc / "meminfo", "MemTotal")
        mem_available = _read_number(self._proc / "meminfo", "MemAvailable")
        swap_total = _read_number(self._proc / "meminfo", "SwapTotal")
        swap_free = _read_number(self._proc / "meminfo", "SwapFree")
        memory_pct = (
            _ratio(mem_total, mem_total - mem_available)
            if mem_total is not None and mem_available is not None
            else None
        )
        swap_pct = (
            _ratio(swap_total, swap_total - swap_free)
            if swap_total is not None and swap_free is not None
            else None
        )
        load = _read_number(self._proc / "loadavg")
        disk = _safe_disk_usage(self._root)
        pressure = _pressure(self._proc / "pressure")
        uptime = _read_number(self._proc / "uptime")
        metrics: dict[str, object] = {
            "cpu_load_1m": _metric(load, "load", 1.0, 2.0),
            "memory_used_pct": _metric(memory_pct, "percent", 80, 95),
            "swap_used_pct": _metric(swap_pct, "percent", 50, 90),
            "disk_used_pct": _metric(disk[0] if disk else None, "percent", 80, 95),
            "inode_used_pct": _metric(disk[1] if disk else None, "percent", 80, 95),
            "uptime_seconds": _metric(uptime, "seconds", None, None),
            "memory_pressure_some_10s": _metric(pressure, "percent", 10, 25),
        }
        services = [self._registered_service(x, stamp) for x in self._service_targets]
        services.extend(self._registered_docker(x, stamp) for x in self._docker_targets)
        workers = self._worker_facts()
        return {"timestamp": stamp, "metrics": metrics, "services": services, "workers": workers}

    def _registered_service(self, target: str, stamp: str) -> dict[str, object]:
        try:
            data = json.loads(self._service_checker.check(target)) if self._service_checker else {}
            status = data.get("status") if isinstance(data, dict) else None
            state = (
                "HEALTHY"
                if status == "healthy"
                else "CRITICAL"
                if status == "unhealthy"
                else "UNKNOWN"
            )
        except Exception:
            state = "UNKNOWN"
        return {"kind": "service", "target_id": target, "status": state, "timestamp": stamp}

    def _registered_docker(self, target: str, stamp: str) -> dict[str, object]:
        try:
            data = (
                json.loads(self._docker_inspector.inspect(target)) if self._docker_inspector else {}
            )
            running = isinstance(data, dict) and data.get("state") == "running"
            health = data.get("health") if isinstance(data, dict) else None
            state = (
                "HEALTHY"
                if running and health in {"healthy", "none"}
                else "CRITICAL"
                if not running or health == "unhealthy"
                else "UNKNOWN"
            )
        except Exception:
            state = "UNKNOWN"
        return {"kind": "docker", "target_id": target, "status": state, "timestamp": stamp}

    def _worker_facts(self) -> list[dict[str, object]]:
        if self._workers is None:
            return []
        safe: list[dict[str, object]] = []
        for item in self._workers():
            pid, cpu, memory = item.get("pid"), item.get("cpu_pct"), item.get("memory_bytes")
            if not isinstance(pid, int) or pid <= 0:
                continue
            row: dict[str, object] = {"pid": pid}
            if isinstance(cpu, (int, float)) and cpu >= 0:
                row["cpu_pct"] = cpu
            if isinstance(memory, int) and memory >= 0:
                row["memory_bytes"] = memory
            for key in ("project_id", "task_id"):
                value = item.get(key)
                if isinstance(value, str) and len(value) <= 80 and value.isprintable():
                    row[key] = value
            safe.append(row)
        return safe


def _ratio(total: float | None, used: float | None) -> float | None:
    return (
        round(100 * used / total, 2) if total and used is not None and 0 <= used <= total else None
    )


def _metric(
    value: float | None, unit: str, warning: float | None, critical: float | None
) -> dict[str, object]:
    if value is not None and warning is not None and critical is not None:
        state = _health(value, warning, critical)
    else:
        state = "HEALTHY" if value is not None else "UNKNOWN"
    return {"value": round(value, 2) if value is not None else None, "unit": unit, "status": state}


def _safe_disk_usage(path: Path) -> tuple[float, float] | None:
    try:
        usage = shutil.disk_usage(path)
        st = os.statvfs(path)
        if usage.total <= 0 or st.f_files <= 0:
            return None
        return round(100 * usage.used / usage.total, 2), round(
            100 * (st.f_files - st.f_ffree) / st.f_files, 2
        )
    except OSError:
        return None


def _pressure(path: Path) -> float | None:
    try:
        for line in (path / "memory").read_text(encoding="ascii").splitlines():
            parts = line.split()
            if parts and parts[0] == "some":
                return float(next(p.split("=", 1)[1] for p in parts[1:] if p.startswith("avg10=")))
    except (OSError, ValueError, StopIteration):
        return None
    return None
