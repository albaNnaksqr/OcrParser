from __future__ import annotations

from datetime import datetime
from typing import Any

from ...models import Manifest
from ..common import json_dumps


def begin_manifest_freeze(
    manifest: Manifest,
    *,
    frozen_at: datetime,
) -> bool:
    manifest.status = "ready"
    if manifest.frozen_at is not None:
        return False
    manifest.frozen_at = frozen_at
    return True


def store_freeze_report(
    manifest: Manifest,
    report: dict[str, Any],
) -> None:
    manifest.freeze_report_json = json_dumps(report)


def mark_manifest_failed(manifest: Manifest) -> None:
    manifest.status = "failed"


def add_materialized_content(
    manifest: Manifest,
    *,
    next_shard_index: int,
    file_count: int,
    total_bytes: int,
) -> None:
    manifest.next_shard_index = next_shard_index
    manifest.file_count = int(manifest.file_count or 0) + file_count
    manifest.total_bytes = int(manifest.total_bytes or 0) + total_bytes


def request_worker_integrity(
    manifest: Manifest,
    *,
    requested_at: datetime,
) -> None:
    manifest.worker_integrity_status = "pending"
    manifest.worker_integrity_requested_at = requested_at
    manifest.worker_integrity_started_at = None
    manifest.worker_integrity_finished_at = None
    manifest.worker_integrity_server_id = None
    manifest.worker_integrity_report_json = "{}"


def claim_worker_integrity(
    manifest: Manifest,
    *,
    server_id: str,
    started_at: datetime,
) -> None:
    manifest.worker_integrity_status = "running"
    manifest.worker_integrity_started_at = started_at
    manifest.worker_integrity_server_id = server_id


def complete_worker_integrity(
    manifest: Manifest,
    *,
    server_id: str,
    finished_at: datetime,
    ok: bool,
    report_json: str,
) -> None:
    manifest.worker_integrity_status = "ok" if ok else "failed"
    manifest.worker_integrity_finished_at = finished_at
    manifest.worker_integrity_server_id = server_id
    manifest.worker_integrity_report_json = report_json


__all__ = [
    "add_materialized_content",
    "begin_manifest_freeze",
    "claim_worker_integrity",
    "complete_worker_integrity",
    "mark_manifest_failed",
    "request_worker_integrity",
    "store_freeze_report",
]
