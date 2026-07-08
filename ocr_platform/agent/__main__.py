"""Command line entrypoint for the OCR platform agent."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from contextlib import suppress
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Optional, TypeVar

from ocr_parser.infra.failure_category import infer_failure_category
from ocr_platform.agent.client import ControlClient
from ocr_platform.agent.config import AgentConfig, parse_args
from ocr_platform.agent.manifest_integrity import build_worker_manifest_integrity_report
from ocr_platform.agent.paths import check_shared_paths
from ocr_platform.agent.resources import collect_system_resources, evaluate_resource_pressure
from ocr_platform.agent.runner import (
    replay_pending_shard_updates,
    resource_paths,
    resource_pressure,
    run_job,
    run_scan_unit,
)


logger = logging.getLogger(__name__)
T = TypeVar("T")


def _resource_paths(config: AgentConfig) -> list[str]:
    return resource_paths(config)


def _resource_pressure(config: AgentConfig) -> dict[str, object]:
    return resource_pressure(config)


def _job_exception_failure_payload(exc: BaseException) -> dict[str, str]:
    error_message = str(exc)
    return {
        "error": error_message,
        "error_message": error_message,
        "failure_category": infer_failure_category({"error_message": error_message}),
    }


def _jsonl_nonblank_line_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as handle:
        return sum(1 for line in handle if line.strip())


def _file_size(path: Path) -> int:
    if not path.exists():
        return 0
    return path.stat().st_size


def _dropped_spool_count(path: Path) -> int:
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return 0
    if not isinstance(payload, dict):
        return 0
    try:
        return max(int(payload.get("dropped") or 0), 0)
    except (TypeError, ValueError):
        return 0


def _event_spool_capabilities(
    spool_dir: str | None,
    *,
    max_pending_bytes: int | None = None,
) -> dict[str, object] | None:
    if not spool_dir:
        return None
    path = Path(spool_dir)
    return {
        "dir": str(path),
        "pending_events": _jsonl_nonblank_line_count(path / "events.jsonl"),
        "pending_logs": _jsonl_nonblank_line_count(path / "logs.jsonl"),
        "failed_events": _jsonl_nonblank_line_count(path / "events.failed.jsonl"),
        "failed_logs": _jsonl_nonblank_line_count(path / "logs.failed.jsonl"),
        "pending_event_bytes": _file_size(path / "events.jsonl"),
        "pending_log_bytes": _file_size(path / "logs.jsonl"),
        "failed_event_bytes": _file_size(path / "events.failed.jsonl"),
        "failed_log_bytes": _file_size(path / "logs.failed.jsonl"),
        "dropped_events": _dropped_spool_count(path / "events.dropped.json"),
        "dropped_logs": _dropped_spool_count(path / "logs.dropped.json"),
        "max_pending_bytes": max_pending_bytes,
    }


def _pending_shard_update_capabilities(work_dir: str) -> dict[str, int]:
    root = Path(work_dir) / "jobs"
    if not root.exists():
        return {"pending": 0, "failed": 0}
    return {
        "pending": sum(1 for _ in root.glob("*/pending-shard-updates/shard-*.json")),
        "failed": sum(1 for _ in root.glob("*/pending-shard-updates/shard-*.json.failed")),
    }


def _heartbeat_capabilities(config: AgentConfig) -> dict[str, object]:
    capabilities: dict[str, object] = {
        "agent": "ocr-platform-mvp",
        "python_path": config.python_executable,
        "repo_dir": config.repo_dir,
        "work_dir": config.work_dir,
        "shared_roots": config.shared_roots or [],
        "poll_interval_seconds": config.poll_interval_seconds,
        "heartbeat_interval_seconds": config.heartbeat_interval_seconds,
        "process_termination_timeout_seconds": config.process_termination_timeout_seconds,
        "stop_poll_interval_seconds": config.stop_poll_interval_seconds,
        "event_spool_dir": config.event_spool_dir,
    }
    event_spool = _event_spool_capabilities(
        config.event_spool_dir,
        max_pending_bytes=config.event_spool_max_bytes,
    )
    if event_spool is not None:
        capabilities["event_spool"] = event_spool
    capabilities["pending_shard_updates"] = _pending_shard_update_capabilities(config.work_dir)
    git_ref = config.git_ref or os.environ.get("OCR_AGENT_GIT_REF")
    script_version = config.script_version or os.environ.get("OCR_AGENT_SCRIPT_VERSION")
    if git_ref:
        capabilities["git_ref"] = git_ref
    if script_version:
        capabilities["script_version"] = script_version
    if config.shared_roots:
        capabilities["shared_paths"] = check_shared_paths(config.shared_roots)
    system_resources = collect_system_resources(_resource_paths(config))
    capabilities["system_resources"] = system_resources
    if config.resource_guard_enabled:
        capabilities["resource_pressure"] = evaluate_resource_pressure(
            system_resources,
            memory_percent_threshold=config.resource_guard_memory_percent,
            min_available_memory_bytes=config.resource_guard_min_available_memory_bytes,
            disk_percent_threshold=config.resource_guard_disk_percent,
            min_free_disk_bytes=config.resource_guard_min_free_disk_bytes,
        )
    else:
        capabilities["resource_pressure"] = {
            "constrained": False,
            "level": "disabled",
            "reasons": [],
        }
    return capabilities


async def _call_control_with_backoff(
    call: Callable[[], Awaitable[T]],
    *,
    operation: str,
    initial_delay: float,
    max_delay: float,
    sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
) -> T:
    delay = max(initial_delay, 0.1)
    max_delay = max(max_delay, delay)
    while True:
        try:
            return await call()
        except Exception as exc:
            logger.warning(
                "Control API %s failed: %s; retrying in %.1fs",
                operation,
                exc,
                delay,
            )
            await sleep(delay)
            delay = min(delay * 2, max_delay)


async def _heartbeat_forever(
    client: ControlClient,
    config: AgentConfig,
    interval_seconds: float,
    status: str,
    current_job_id: Optional[str] = None,
) -> None:
    while True:
        try:
            await _heartbeat_once(client, config, status, current_job_id=current_job_id)
        except Exception:
            pass
        await asyncio.sleep(interval_seconds)


async def _heartbeat_once(
    client: ControlClient,
    config: AgentConfig,
    status: str,
    current_job_id: Optional[str] = None,
) -> None:
    await client.heartbeat(
        status=status,
        current_job_id=current_job_id,
        capabilities=_heartbeat_capabilities(config),
    )
    replay = getattr(client, "replay_spooled_events", None)
    replayed_records = 0
    if callable(replay):
        replayed_records += int(await replay() or 0)
    replay_logs = getattr(client, "replay_spooled_logs", None)
    if callable(replay_logs):
        replayed_records += int(await replay_logs() or 0)
    replayed_records += int(await replay_pending_shard_updates(config, client) or 0)
    if replayed_records > 0:
        await client.heartbeat(
            status=status,
            current_job_id=current_job_id,
            capabilities=_heartbeat_capabilities(config),
        )


async def _stop_heartbeat(task: asyncio.Task[None]) -> None:
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


async def _process_scan_unit(
    scan_unit: dict[str, object],
    client: ControlClient,
    config: AgentConfig,
) -> None:
    job_id = str(scan_unit["job_id"])
    try:
        await _heartbeat_once(client, config, "busy", current_job_id=job_id)
    except Exception:
        pass
    heartbeat_task = asyncio.create_task(
        _heartbeat_forever(
            client,
            config,
            config.heartbeat_interval_seconds,
            "busy",
            job_id,
        )
    )
    try:
        await run_scan_unit(scan_unit, config, client)
    finally:
        await _stop_heartbeat(heartbeat_task)
        try:
            await _heartbeat_once(client, config, "idle")
        except Exception:
            pass


async def _run_scan_once(client: ControlClient, config: AgentConfig) -> bool:
    pressure = _resource_pressure(config)
    if pressure.get("constrained"):
        logger.info("Skipping scan unit claim due to resource pressure: %s", pressure["reasons"])
        return False
    scan_unit = await _call_control_with_backoff(
        lambda: client.claim_scan_unit(config.server_id),
        operation="claim_scan_unit",
        initial_delay=config.control_retry_initial_seconds,
        max_delay=config.control_retry_max_seconds,
    )
    if scan_unit is None:
        return False
    await _process_scan_unit(scan_unit, client, config)
    return True


async def _next_job_if_resources_allow(
    client: ControlClient,
    config: AgentConfig,
) -> Optional[dict[str, object]]:
    pressure = _resource_pressure(config)
    if pressure.get("constrained"):
        logger.info("Skipping job claim due to resource pressure: %s", pressure["reasons"])
        return None
    return await _call_control_with_backoff(
        client.next_job,
        operation="next_job",
        initial_delay=config.control_retry_initial_seconds,
        max_delay=config.control_retry_max_seconds,
    )


async def _scan_lane_forever(client: ControlClient, config: AgentConfig) -> None:
    while True:
        did_work = await _run_scan_once(client, config)
        await asyncio.sleep(0 if did_work else config.poll_interval_seconds)


async def _run_manifest_integrity_once(client: ControlClient, config: AgentConfig) -> bool:
    pressure = _resource_pressure(config)
    if pressure.get("constrained"):
        logger.info(
            "Skipping manifest integrity claim due to resource pressure: %s",
            pressure["reasons"],
        )
        return False
    task = await _call_control_with_backoff(
        lambda: client.claim_manifest_integrity(config.server_id),
        operation="claim_manifest_integrity",
        initial_delay=config.control_retry_initial_seconds,
        max_delay=config.control_retry_max_seconds,
    )
    if task is None:
        return False
    report = build_worker_manifest_integrity_report(task)
    await _call_control_with_backoff(
        lambda: client.complete_manifest_integrity(
            int(task["manifest_id"]),
            {"report": report},
            config.server_id,
        ),
        operation="complete_manifest_integrity",
        initial_delay=config.control_retry_initial_seconds,
        max_delay=config.control_retry_max_seconds,
    )
    return True


async def _manifest_integrity_lane_forever(client: ControlClient, config: AgentConfig) -> None:
    while True:
        did_work = await _run_manifest_integrity_once(client, config)
        await asyncio.sleep(0 if did_work else config.poll_interval_seconds)


async def amain(argv: Optional[list[str]] = None) -> None:
    config = parse_args(argv)
    logging.basicConfig(level=os.environ.get("OCR_AGENT_LOG_LEVEL", "INFO"))
    client = ControlClient(
        config.control_url,
        config.server_id,
        api_token=config.control_api_token,
        event_spool_dir=config.event_spool_dir,
        event_spool_max_bytes=config.event_spool_max_bytes,
    )
    scan_lane_task: asyncio.Task[None] | None = None
    manifest_integrity_lane_task: asyncio.Task[None] | None = None
    try:
        await _call_control_with_backoff(
            lambda: client.register(host=socket.gethostname()),
            operation="register",
            initial_delay=config.control_retry_initial_seconds,
            max_delay=config.control_retry_max_seconds,
        )
        scan_lane_task = asyncio.create_task(_scan_lane_forever(client, config))
        manifest_integrity_lane_task = asyncio.create_task(
            _manifest_integrity_lane_forever(client, config)
        )
        while True:
            try:
                await _heartbeat_once(client, config, "idle")
            except Exception:
                pass
            job = await _next_job_if_resources_allow(client, config)
            if job is None:
                await asyncio.sleep(config.poll_interval_seconds)
                continue
            if job is not None:
                try:
                    await _heartbeat_once(client, config, "busy", current_job_id=str(job["id"]))
                except Exception:
                    pass
                heartbeat_task = asyncio.create_task(
                    _heartbeat_forever(
                        client,
                        config,
                        config.heartbeat_interval_seconds,
                        "busy",
                        str(job["id"]),
                    )
                )
                try:
                    await run_job(job, config, client)
                except Exception as exc:
                    # Keep the long-running agent alive and best-effort the
                    # terminal state for any claimed job that escaped run_job.
                    try:
                        await client.post_event(
                            str(job["id"]),
                            {
                                "type": "job_failed",
                                "payload": _job_exception_failure_payload(exc),
                            },
                        )
                    except Exception:
                        pass
                finally:
                    await _stop_heartbeat(heartbeat_task)
                    try:
                        await _heartbeat_once(client, config, "idle")
                    except Exception:
                        pass
            await asyncio.sleep(config.poll_interval_seconds)
    finally:
        if scan_lane_task is not None:
            scan_lane_task.cancel()
            with suppress(asyncio.CancelledError):
                await scan_lane_task
        if manifest_integrity_lane_task is not None:
            manifest_integrity_lane_task.cancel()
            with suppress(asyncio.CancelledError):
                await manifest_integrity_lane_task
        await client.close()


def main() -> None:
    asyncio.run(amain())


if __name__ == "__main__":
    main()
