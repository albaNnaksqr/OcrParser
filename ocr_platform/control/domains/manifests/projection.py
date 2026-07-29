"""Pure Manifest, shard, and attempt read projections."""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import Job, JobEvent, Manifest, ScanUnit, ShardAttempt, WorkShard
from ...schemas import ShardAttemptListResponse, ShardAttemptResponse
from ..common import (
    ATTENTION_SHARD_STATUSES,
    SHARD_STATUS_FILTERS,
    UnknownJobError,
    _scan_error_sample_with_category,
    json_loads_object,
    utcnow,
)


def get_job_or_raise(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise UnknownJobError(f"unknown job: {job_id}")
    return job


def latest_manifest_scan_progress(
    session: Session,
    job_id: str,
) -> dict[str, Any]:
    event = session.execute(
        select(JobEvent)
        .where(JobEvent.job_id == job_id)
        .where(JobEvent.event_type == "manifest_scan_progress")
        .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if event is None:
        return {}
    return json_loads_object(event.payload_json)


def manifest_scan_metadata(
    manifest: Manifest | None,
) -> dict[str, Any]:
    if manifest is None or not manifest.meta_path:
        return {}
    try:
        payload = json.loads(
            Path(manifest.meta_path).read_text(encoding="utf-8")
        )
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def manifest_scan_error_samples(
    manifest_meta: dict[str, Any],
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for item in manifest_meta.get("skipped_errors") or []:
        if not isinstance(item, dict):
            continue
        samples.append(_scan_error_sample_with_category(item))
        if len(samples) >= limit:
            break
    return samples


def recent_manifest_scan_error_samples(
    session: Session,
    job_id: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    rows = session.execute(
        select(JobEvent)
        .where(JobEvent.job_id == job_id)
        .where(JobEvent.event_type == "manifest_scan_progress")
        .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
        .limit(50)
    ).scalars().all()
    samples: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for event in rows:
        payload = json_loads_object(event.payload_json)
        for item in payload.get("skipped_errors") or []:
            if not isinstance(item, dict):
                continue
            sample = _scan_error_sample_with_category(item)
            key = (
                str(sample.get("path") or ""),
                str(sample.get("reason") or ""),
            )
            if key in seen_keys:
                continue
            seen_keys.add(key)
            samples.append(sample)
            if len(samples) >= limit:
                return samples
    return samples


def scan_unit_problem_samples(
    session: Session,
    job_id: str,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "reason": error_message or f"scan unit {status}",
            "failure_category": failure_category,
        }
        for path, status, error_message, failure_category in session.execute(
            select(
                ScanUnit.path,
                ScanUnit.status,
                ScanUnit.error_message,
                ScanUnit.failure_category,
            )
            .where(ScanUnit.job_id == job_id)
            .where(ScanUnit.status.in_({"failed", "stale"}))
            .order_by(ScanUnit.id.asc())
            .limit(limit)
        ).all()
    ]
def _normalized_shard_status_filter(status: str | None) -> str:
    normalized = status.strip().lower() if status else "all"
    if not normalized:
        normalized = "all"
    if normalized not in SHARD_STATUS_FILTERS:
        allowed = ", ".join(sorted(SHARD_STATUS_FILTERS))
        raise ValueError(
            f"unknown shard status filter: {status}; allowed values: {allowed}"
        )
    return normalized

def list_work_shards(
    session: Session,
    job_id: str,
    *,
    status: str = "all",
    worker_id: str | None = None,
    failure_category: str | None = None,
    min_attempt_count: int | None = None,
    running_longer_than_seconds: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[WorkShard], int]:
    get_job_or_raise(session, job_id)
    status_filter = _normalized_shard_status_filter(status)
    filters = [WorkShard.job_id == job_id]
    if status_filter == "attention":
        filters.append(WorkShard.status.in_(ATTENTION_SHARD_STATUSES))
    elif status_filter != "all":
        filters.append(WorkShard.status == status_filter)
    if worker_id:
        filters.append(WorkShard.assigned_server_id == worker_id)
    if failure_category:
        filters.append(WorkShard.failure_category == failure_category)
    if min_attempt_count is not None:
        filters.append(WorkShard.attempt_count >= min_attempt_count)
    if running_longer_than_seconds is not None:
        threshold = utcnow() - timedelta(seconds=running_longer_than_seconds)
        filters.append(WorkShard.status == "running")
        filters.append(WorkShard.started_at.is_not(None))
        filters.append(WorkShard.started_at <= threshold)
    total = int(
        session.execute(
            select(func.count(WorkShard.id)).where(*filters)
        ).scalar_one()
        or 0
    )
    stmt = (
        select(WorkShard)
        .where(*filters)
        .order_by(WorkShard.shard_index.asc())
        .offset(max(offset, 0))
        .limit(max(limit, 1))
    )
    return list(session.execute(stmt).scalars().all()), total

def has_static_shards(session: Session, job_id: str) -> bool:
    return bool(
        session.execute(
            select(func.count(WorkShard.id)).where(WorkShard.job_id == job_id)
        ).scalar_one()
    )

def list_shard_attempts(
    session: Session,
    job_id: str,
    shard_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[ShardAttempt]:
    get_job_or_raise(session, job_id)
    shard = session.get(WorkShard, shard_id)
    if shard is None or shard.job_id != job_id:
        raise ValueError(f"unknown shard for job: {shard_id}")
    return list(
        session.execute(
            select(ShardAttempt)
            .where(ShardAttempt.shard_id == shard_id)
            .order_by(ShardAttempt.attempt_number.asc(), ShardAttempt.id.asc())
            .offset(max(offset, 0))
            .limit(max(limit, 1))
        ).scalars().all()
    )

def list_shard_attempts_page(
    session: Session,
    job_id: str,
    shard_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> ShardAttemptListResponse:
    attempts = list_shard_attempts(
        session,
        job_id,
        shard_id,
        limit=limit,
        offset=offset,
    )
    total = int(
        session.execute(
            select(func.count(ShardAttempt.id)).where(ShardAttempt.shard_id == shard_id)
        ).scalar_one()
        or 0
    )
    bounded_offset = max(offset, 0)
    bounded_limit = max(limit, 1)
    return ShardAttemptListResponse(
        job_id=job_id,
        shard_id=shard_id,
        total=total,
        limit=bounded_limit,
        offset=bounded_offset,
        has_more=bounded_offset + len(attempts) < total,
        items=[shard_attempt_to_response(attempt) for attempt in attempts],
    )

def shard_attempt_to_response(attempt: ShardAttempt) -> ShardAttemptResponse:
    return ShardAttemptResponse(
        id=attempt.id,
        job_id=attempt.job_id,
        shard_id=attempt.shard_id,
        attempt_number=attempt.attempt_number,
        server_id=attempt.server_id,
        status=attempt.status,
        processed_files=attempt.processed_files,
        failed_files=attempt.failed_files,
        skipped_files=attempt.skipped_files,
        completed_pages=attempt.completed_pages,
        execution_paused=attempt.execution_paused,
        api_concurrency_limit=attempt.api_concurrency_limit,
        execution_control_reason=attempt.execution_control_reason,
        failure_category=attempt.failure_category,
        error_message=attempt.error_message,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )

normalize_shard_status_filter = _normalized_shard_status_filter

__all__ = [name for name in globals() if not name.startswith("__")]
