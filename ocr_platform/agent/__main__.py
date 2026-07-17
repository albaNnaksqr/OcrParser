"""Command line entrypoint for the OCR platform agent."""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Optional

from ocr_platform.optional import PLATFORM_MODULES, require_extra

from . import lanes as _lanes
from .client import ControlClient
from .config import AgentConfig, parse_args
from .lanes import *
from .manifest_integrity import build_worker_manifest_integrity_report
from .runner import replay_pending_shard_updates, run_scan_unit
from .runtime import AgentRuntime


# Compatibility wrappers preserve the v0.2 helper monkeypatch surface while the
# runtime itself imports and supervises the owning lane implementations.
async def _heartbeat_once(
    client: ControlClient,
    config: AgentConfig,
    status: str,
    current_job_id: Optional[str] = None,
) -> None:
    original = _lanes.replay_pending_shard_updates
    _lanes.replay_pending_shard_updates = replay_pending_shard_updates
    try:
        await _lanes._heartbeat_once(client, config, status, current_job_id)
    finally:
        _lanes.replay_pending_shard_updates = original


async def _run_scan_once(client: ControlClient, config: AgentConfig) -> bool:
    original_run = _lanes.run_scan_unit
    original_pressure = _lanes._resource_pressure
    _lanes.run_scan_unit = run_scan_unit
    _lanes._resource_pressure = _resource_pressure
    try:
        return await _lanes._run_scan_once(client, config)
    finally:
        _lanes.run_scan_unit = original_run
        _lanes._resource_pressure = original_pressure


async def _next_job_if_resources_allow(client: ControlClient, config: AgentConfig):
    original = _lanes._resource_pressure
    _lanes._resource_pressure = _resource_pressure
    try:
        return await _lanes._next_job_if_resources_allow(client, config)
    finally:
        _lanes._resource_pressure = original


async def _run_manifest_integrity_once(client: ControlClient, config: AgentConfig) -> bool:
    original_reporter = _lanes.build_worker_manifest_integrity_report
    original_pressure = _lanes._resource_pressure
    _lanes.build_worker_manifest_integrity_report = build_worker_manifest_integrity_report
    _lanes._resource_pressure = _resource_pressure
    try:
        return await _lanes._run_manifest_integrity_once(client, config)
    finally:
        _lanes.build_worker_manifest_integrity_report = original_reporter
        _lanes._resource_pressure = original_pressure


async def amain(argv: Optional[list[str]] = None) -> None:
    config = parse_args(argv)
    logging.basicConfig(level=os.environ.get("OCR_AGENT_LOG_LEVEL", "INFO"))
    await AgentRuntime(config).run()


def main() -> None:
    require_extra("platform", PLATFORM_MODULES)
    asyncio.run(amain())


if __name__ == "__main__":
    main()
