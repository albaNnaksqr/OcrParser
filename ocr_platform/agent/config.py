"""Configuration for the OCR platform agent."""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from typing import List, Optional


DEFAULT_EVENT_SPOOL_MAX_BYTES = 256 * 1024**2


@dataclass(frozen=True)
class AgentConfig:
    server_id: str
    control_url: str
    control_api_token: str | None = None
    work_dir: str = ".ocr-agent"
    poll_interval_seconds: float = 5.0
    heartbeat_interval_seconds: float = 10.0
    control_retry_initial_seconds: float = 1.0
    control_retry_max_seconds: float = 30.0
    event_spool_dir: str | None = None
    event_spool_max_bytes: int = DEFAULT_EVENT_SPOOL_MAX_BYTES
    process_termination_timeout_seconds: float = 5.0
    stop_poll_interval_seconds: float = 1.0
    shared_roots: list[str] | None = None
    python_executable: str = sys.executable
    repo_dir: str = "."
    git_ref: str | None = None
    script_version: str | None = None
    resource_guard_enabled: bool = True
    resource_guard_memory_percent: float = 90.0
    resource_guard_min_available_memory_bytes: int = 4 * 1024**3
    resource_guard_disk_percent: float = 95.0
    resource_guard_min_free_disk_bytes: int = 10 * 1024**3


def parse_args(argv: Optional[List[str]] = None) -> AgentConfig:
    parser = argparse.ArgumentParser(description="Run an OCR platform agent.")
    parser.add_argument(
        "--server_id",
        default=os.environ.get("OCR_AGENT_SERVER_ID", "local"),
    )
    parser.add_argument(
        "--control_url",
        default=os.environ.get("OCR_CONTROL_URL", "http://127.0.0.1:8080"),
    )
    parser.add_argument(
        "--control_api_token",
        default=os.environ.get("OCR_CONTROL_API_TOKEN"),
    )
    parser.add_argument(
        "--work_dir",
        default=os.environ.get("OCR_AGENT_WORK_DIR", ".ocr-agent"),
    )
    parser.add_argument(
        "--poll_interval_seconds",
        type=float,
        default=float(os.environ.get("OCR_AGENT_POLL_INTERVAL", "5")),
    )
    parser.add_argument(
        "--heartbeat_interval_seconds",
        type=float,
        default=float(os.environ.get("OCR_AGENT_HEARTBEAT_INTERVAL", "10")),
    )
    parser.add_argument(
        "--control_retry_initial_seconds",
        type=float,
        default=float(os.environ.get("OCR_AGENT_CONTROL_RETRY_INITIAL", "1")),
    )
    parser.add_argument(
        "--control_retry_max_seconds",
        type=float,
        default=float(os.environ.get("OCR_AGENT_CONTROL_RETRY_MAX", "30")),
    )
    parser.add_argument(
        "--event_spool_dir",
        default=os.environ.get("OCR_AGENT_EVENT_SPOOL_DIR"),
    )
    parser.add_argument(
        "--disable_event_spool",
        action="store_true",
        default=os.environ.get("OCR_AGENT_DISABLE_EVENT_SPOOL", "").lower()
        in {"1", "true", "yes"},
    )
    parser.add_argument(
        "--event_spool_max_mb",
        type=float,
        default=float(os.environ.get("OCR_AGENT_EVENT_SPOOL_MAX_MB", "256")),
        help="Maximum bytes, in MiB, kept in each pending event/log spool file. Use 0 for unlimited.",
    )
    parser.add_argument(
        "--process_termination_timeout_seconds",
        type=float,
        default=float(os.environ.get("OCR_AGENT_TERMINATION_TIMEOUT", "5")),
    )
    parser.add_argument(
        "--stop_poll_interval_seconds",
        type=float,
        default=float(os.environ.get("OCR_AGENT_STOP_POLL_INTERVAL", "1")),
    )
    parser.add_argument(
        "--shared_root",
        action="append",
        default=None,
        help="Shared filesystem root to probe and report in heartbeat. Repeatable.",
    )
    parser.add_argument(
        "--python_executable",
        default=os.environ.get("OCR_AGENT_PYTHON", sys.executable),
    )
    parser.add_argument(
        "--repo_dir",
        default=os.environ.get("OCR_REPO_DIR", os.getcwd()),
    )
    parser.add_argument(
        "--git_ref",
        default=os.environ.get("OCR_AGENT_GIT_REF"),
    )
    parser.add_argument(
        "--script_version",
        default=os.environ.get("OCR_AGENT_SCRIPT_VERSION"),
    )
    parser.add_argument(
        "--disable_resource_guard",
        action="store_true",
        default=os.environ.get("OCR_AGENT_DISABLE_RESOURCE_GUARD", "").lower()
        in {"1", "true", "yes"},
    )
    parser.add_argument(
        "--resource_guard_memory_percent",
        type=float,
        default=float(os.environ.get("OCR_AGENT_RESOURCE_GUARD_MEMORY_PERCENT", "90")),
    )
    parser.add_argument(
        "--resource_guard_min_available_memory_gb",
        type=float,
        default=float(os.environ.get("OCR_AGENT_RESOURCE_GUARD_MIN_AVAILABLE_MEMORY_GB", "4")),
    )
    parser.add_argument(
        "--resource_guard_disk_percent",
        type=float,
        default=float(os.environ.get("OCR_AGENT_RESOURCE_GUARD_DISK_PERCENT", "95")),
    )
    parser.add_argument(
        "--resource_guard_min_free_disk_gb",
        type=float,
        default=float(os.environ.get("OCR_AGENT_RESOURCE_GUARD_MIN_FREE_DISK_GB", "10")),
    )
    args = parser.parse_args(argv)
    env_shared_roots = [
        item
        for item in os.environ.get("OCR_AGENT_SHARED_ROOTS", "").split(os.pathsep)
        if item
    ]
    shared_roots = args.shared_root if args.shared_root is not None else env_shared_roots
    event_spool_dir = None
    if not args.disable_event_spool:
        event_spool_dir = args.event_spool_dir or os.path.join(args.work_dir, "event-spool")

    return AgentConfig(
        server_id=args.server_id,
        control_url=args.control_url.rstrip("/"),
        control_api_token=args.control_api_token,
        work_dir=args.work_dir,
        poll_interval_seconds=args.poll_interval_seconds,
        heartbeat_interval_seconds=args.heartbeat_interval_seconds,
        control_retry_initial_seconds=args.control_retry_initial_seconds,
        control_retry_max_seconds=args.control_retry_max_seconds,
        event_spool_dir=event_spool_dir,
        event_spool_max_bytes=int(args.event_spool_max_mb * 1024**2),
        process_termination_timeout_seconds=args.process_termination_timeout_seconds,
        stop_poll_interval_seconds=args.stop_poll_interval_seconds,
        shared_roots=shared_roots,
        python_executable=args.python_executable,
        repo_dir=args.repo_dir,
        git_ref=args.git_ref,
        script_version=args.script_version,
        resource_guard_enabled=not args.disable_resource_guard,
        resource_guard_memory_percent=args.resource_guard_memory_percent,
        resource_guard_min_available_memory_bytes=int(
            args.resource_guard_min_available_memory_gb * 1024**3
        ),
        resource_guard_disk_percent=args.resource_guard_disk_percent,
        resource_guard_min_free_disk_bytes=int(args.resource_guard_min_free_disk_gb * 1024**3),
    )
