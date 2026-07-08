"""Host resource probes reported by the OCR platform agent."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DEFAULT_MEMORY_PERCENT_THRESHOLD = 90.0
DEFAULT_MIN_AVAILABLE_MEMORY_BYTES = 4 * 1024**3
DEFAULT_DISK_PERCENT_THRESHOLD = 95.0
DEFAULT_MIN_FREE_DISK_BYTES = 10 * 1024**3


def _load_average() -> tuple[float | None, float | None, float | None]:
    try:
        return tuple(float(item) for item in os.getloadavg())
    except (AttributeError, OSError):
        return (None, None, None)


def _linux_memory() -> dict[str, int] | None:
    meminfo = Path("/proc/meminfo")
    if not meminfo.exists():
        return None
    values: dict[str, int] = {}
    try:
        for line in meminfo.read_text().splitlines():
            key, raw_value = line.split(":", 1)
            parts = raw_value.strip().split()
            if parts:
                values[key] = int(parts[0]) * 1024
    except (OSError, ValueError):
        return None
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    return {"total_bytes": total, "available_bytes": available}


def _memory_snapshot() -> dict[str, float | int]:
    memory = _linux_memory() or {"total_bytes": 0, "available_bytes": 0}
    total = int(memory["total_bytes"])
    available = int(memory["available_bytes"])
    used = max(total - available, 0)
    percent = round((used / total) * 100, 2) if total else 0.0
    return {
        "total_bytes": total,
        "available_bytes": available,
        "used_bytes": used,
        "percent": percent,
    }


def _disk_snapshot(path: str) -> dict[str, Any]:
    exists = Path(path).exists()
    target = path if exists else str(Path(path).parent or ".")
    try:
        usage = shutil.disk_usage(target)
    except OSError:
        usage = shutil.disk_usage("/")
    used = usage.total - usage.free
    percent = round((used / usage.total) * 100, 2) if usage.total else 0.0
    return {
        "path": path,
        "exists": exists,
        "total_bytes": usage.total,
        "free_bytes": usage.free,
        "used_bytes": used,
        "percent": percent,
    }


def collect_system_resources(paths: list[str] | None = None) -> dict[str, Any]:
    """Collect lightweight host metrics without requiring optional packages."""

    logical_count = os.cpu_count() or 1
    load_1m, load_5m, load_15m = _load_average()
    load_percent = round((load_1m / logical_count) * 100, 2) if load_1m is not None else None
    unique_paths = list(dict.fromkeys(paths or []))
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "cpu": {
            "logical_count": logical_count,
            "load_avg_1m": load_1m,
            "load_avg_5m": load_5m,
            "load_avg_15m": load_15m,
            "load_percent_1m": load_percent,
        },
        "memory": _memory_snapshot(),
        "disks": [_disk_snapshot(path) for path in unique_paths],
    }


def evaluate_resource_pressure(
    resources: dict[str, Any],
    *,
    memory_percent_threshold: float = DEFAULT_MEMORY_PERCENT_THRESHOLD,
    min_available_memory_bytes: int = DEFAULT_MIN_AVAILABLE_MEMORY_BYTES,
    disk_percent_threshold: float = DEFAULT_DISK_PERCENT_THRESHOLD,
    min_free_disk_bytes: int = DEFAULT_MIN_FREE_DISK_BYTES,
) -> dict[str, Any]:
    """Return whether local resources should pause new work claims."""

    reasons: list[str] = []
    memory = resources.get("memory") or {}
    memory_percent = memory.get("percent")
    available_memory = memory.get("available_bytes")
    if isinstance(memory_percent, (int, float)) and memory_percent >= memory_percent_threshold:
        reasons.append(
            f"memory percent {memory_percent:.1f}% >= {memory_percent_threshold:.1f}%"
        )
    if (
        isinstance(available_memory, (int, float))
        and available_memory > 0
        and available_memory < min_available_memory_bytes
    ):
        reasons.append(
            f"available memory {int(available_memory)} < {int(min_available_memory_bytes)} bytes"
        )

    for disk in resources.get("disks") or []:
        path = str(disk.get("path") or "disk")
        disk_percent = disk.get("percent")
        free_bytes = disk.get("free_bytes")
        if isinstance(disk_percent, (int, float)) and disk_percent >= disk_percent_threshold:
            reasons.append(
                f"{path} disk percent {disk_percent:.1f}% >= {disk_percent_threshold:.1f}%"
            )
        if (
            isinstance(free_bytes, (int, float))
            and free_bytes > 0
            and free_bytes < min_free_disk_bytes
        ):
            reasons.append(
                f"{path} disk free {int(free_bytes)} < {int(min_free_disk_bytes)} bytes"
            )

    return {
        "constrained": bool(reasons),
        "level": "blocked" if reasons else "ready",
        "reasons": reasons,
        "thresholds": {
            "memory_percent": memory_percent_threshold,
            "min_available_memory_bytes": min_available_memory_bytes,
            "disk_percent": disk_percent_threshold,
            "min_free_disk_bytes": min_free_disk_bytes,
        },
    }
