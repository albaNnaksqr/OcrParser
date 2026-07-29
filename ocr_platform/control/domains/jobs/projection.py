from __future__ import annotations

import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ocr_parser.infra.failure_category import infer_failure_category
from sqlalchemy import Integer, case, delete, distinct, func, select
from sqlalchemy.orm import Session

from ...limits import ControlLimits as __ControlLimits
from ...limits import legacy_control_limits as __legacy_control_limits
from ...models import Job, JobCounter, JobEvent, JobFile, JobLog, Manifest, ScanUnit, Server, WorkShard
from ...schemas import JobEventRequest, JobRecentErrorListResponse, JobRecentErrorResponse, JobShardProgressSummary, JobSummaryListResponse, JobSummaryResponse, JobWorkerShardSummary
from ..common import *

def _job_worker_version_summary(*args, **kwargs):
    from ..workers.core import _job_worker_version_summary as target
    return target(*args, **kwargs)

def _load_worker_integrity_report(*args, **kwargs):
    from ..manifests.integrity import load_worker_integrity_report as target
    return target(*args, **kwargs)

def _manifest_integrity_freeze_summary(*args, **kwargs):
    from ..manifests.integrity import manifest_integrity_freeze_summary as target
    return target(*args, **kwargs)

def allowed_server_ids_for_job(*args, **kwargs):
    from ..workers.core import allowed_server_ids_for_job as target
    return target(*args, **kwargs)

def public_assigned_server_id(*args, **kwargs):
    from ..workers.core import public_assigned_server_id as target
    return target(*args, **kwargs)

from .counters import (
    _job_counter_total_files,
    _load_failure_category_counts,
    _load_recent_error_samples,
    _load_recent_failed_file_samples,
    _optional_int,
)
from .lifecycle import get_or_raise as get_job_or_raise
def _normalized_status_filter(status: str | None) -> str | None:
    from .lifecycle import normalize_status_filter

    return normalize_status_filter(status)

def list_job_summaries(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
    limits: __ControlLimits | None = None,
) -> list[JobSummaryResponse]:
    control_limits = (
        limits if limits is not None else __legacy_control_limits()
    )
    stmt = select(Job).order_by(Job.created_at.desc())
    status = _normalized_status_filter(status)
    if status:
        stmt = stmt.where(Job.status == status)
    if not include_archived:
        stmt = stmt.where(Job.archived_at.is_(None))
    stmt = stmt.offset(max(offset, 0)).limit(max(limit, 1))
    jobs = session.execute(stmt).scalars().all()
    return [
        get_job_summary(session, job, limits=control_limits)
        for job in jobs
    ]

def list_job_summaries_page(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
    limits: __ControlLimits | None = None,
) -> JobSummaryListResponse:
    control_limits = (
        limits if limits is not None else __legacy_control_limits()
    )
    limit = max(limit, 1)
    offset = max(offset, 0)
    status = _normalized_status_filter(status)
    count_stmt = select(func.count(Job.id))
    item_stmt = select(Job).order_by(Job.created_at.desc())
    if status:
        count_stmt = count_stmt.where(Job.status == status)
        item_stmt = item_stmt.where(Job.status == status)
    if not include_archived:
        count_stmt = count_stmt.where(Job.archived_at.is_(None))
        item_stmt = item_stmt.where(Job.archived_at.is_(None))
    total = int(session.execute(count_stmt).scalar_one() or 0)
    jobs = session.execute(item_stmt.offset(offset).limit(limit)).scalars().all()
    return JobSummaryListResponse(
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(jobs) < total,
        items=[
            get_job_summary(session, job, limits=control_limits)
            for job in jobs
        ],
    )

def _static_input_file_count(session: Session, job_id: str) -> int:
    manifest_file_count = session.execute(
        select(func.max(Manifest.file_count)).where(Manifest.job_id == job_id)
    ).scalar_one()
    shard_file_count = session.execute(
        select(func.coalesce(func.sum(WorkShard.file_count), 0)).where(
            WorkShard.job_id == job_id
        )
    ).scalar_one()
    return max(int(manifest_file_count or 0), int(shard_file_count or 0))

def _latest_manifest_scan_progress(session: Session, job_id: str) -> dict[str, Any]:
    from ..manifests.projection import latest_manifest_scan_progress

    return latest_manifest_scan_progress(session, job_id)

def _manifest_scan_metadata(manifest: Manifest | None) -> dict[str, Any]:
    from ..manifests.projection import manifest_scan_metadata

    return manifest_scan_metadata(manifest)

def _manifest_scan_error_samples(manifest_meta: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    from ..manifests.projection import manifest_scan_error_samples

    return manifest_scan_error_samples(manifest_meta, limit=limit)

def _recent_manifest_scan_error_samples(session: Session, job_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    from ..manifests.projection import recent_manifest_scan_error_samples

    return recent_manifest_scan_error_samples(
        session,
        job_id,
        limit=limit,
    )

def _scan_unit_problem_samples(session: Session, job_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    from ..manifests.projection import scan_unit_problem_samples

    return scan_unit_problem_samples(session, job_id, limit=limit)

def _manifest_scan_started_at(session: Session, job_id: str) -> datetime | None:
    return session.execute(
        select(func.min(JobEvent.created_at))
        .where(JobEvent.job_id == job_id)
        .where(JobEvent.event_type == "manifest_scan_progress")
    ).scalar_one()

def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)

def _scan_eta_seconds(
    *,
    started_at: datetime | None,
    now: datetime,
    scanned_files: int,
    estimated_total_files: int | None,
) -> int | None:
    if started_at is None or estimated_total_files is None:
        return None
    if scanned_files <= 0 or estimated_total_files <= scanned_files:
        return None
    elapsed_seconds = max((now - started_at).total_seconds(), 0.0)
    if elapsed_seconds <= 0:
        return None
    files_per_second = scanned_files / elapsed_seconds
    if files_per_second <= 0:
        return None
    return int((estimated_total_files - scanned_files) / files_per_second)

def _scan_eta_seconds_from_rate(
    *,
    scanned_files: int,
    estimated_total_files: int | None,
    files_per_second: Any,
) -> int | None:
    if estimated_total_files is None:
        return None
    if scanned_files <= 0 or estimated_total_files <= scanned_files:
        return None
    try:
        rate = float(files_per_second)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate <= 0:
        return None
    return int((estimated_total_files - scanned_files) / rate)

def _scan_unit_eta_seconds(
    *,
    started_at: datetime | None,
    now: datetime,
    completed_units: int,
    total_units: int,
) -> int | None:
    if started_at is None or completed_units <= 0 or total_units <= completed_units:
        return None
    elapsed_seconds = max((now - started_at).total_seconds(), 0.0)
    if elapsed_seconds <= 0:
        return None
    units_per_second = completed_units / elapsed_seconds
    if units_per_second <= 0:
        return None
    return int((total_units - completed_units) / units_per_second)

def _shard_lease_status(shard: WorkShard, now: datetime) -> tuple[str, int | None]:
    if shard.status == "stale":
        return "stale", 0
    if shard.status not in CURRENT_WORKER_SHARD_STATUSES:
        return "none", None
    if shard.lease_expires_at is None:
        return "missing", None
    remaining = int((shard.lease_expires_at - now).total_seconds())
    if remaining <= 0:
        return "expired", 0
    if remaining <= 30:
        return "expiring", remaining
    return "healthy", remaining

def _shard_progress_summary(shard: WorkShard, job: Job, now: datetime) -> JobShardProgressSummary:
    lease_status, lease_seconds_remaining = _shard_lease_status(shard, now)
    elapsed_seconds = 0.0
    if shard.started_at is not None:
        elapsed_seconds = max((now - shard.started_at).total_seconds(), 0.0)
    pages_per_second = None
    files_per_minute = None
    if elapsed_seconds > 0:
        if shard.completed_pages > 0:
            pages_per_second = round(shard.completed_pages / elapsed_seconds, 4)
        if shard.processed_files > 0:
            files_per_minute = round(shard.processed_files / elapsed_seconds * 60, 4)
    return JobShardProgressSummary(
        id=shard.id,
        shard_index=shard.shard_index,
        status=shard.status,
        assigned_server_id=shard.assigned_server_id,
        started_at=shard.started_at,
        running_seconds=round(elapsed_seconds, 1) if shard.started_at is not None else None,
        file_count=shard.file_count,
        processed_files=shard.processed_files,
        failed_files=shard.failed_files,
        skipped_files=shard.skipped_files,
        completed_pages=shard.completed_pages,
        api_inflight=shard.api_inflight,
        api_inflight_peak=shard.api_inflight_peak,
        api_waiting=shard.api_waiting,
        oldest_api_inflight=round(shard.oldest_api_inflight, 4),
        execution_paused=shard.execution_paused,
        api_concurrency_limit=shard.api_concurrency_limit,
        execution_control_reason=shard.execution_control_reason,
        pages_per_second=pages_per_second,
        files_per_minute=files_per_minute,
        attempt_count=shard.attempt_count,
        max_attempts=job.max_shard_attempts,
        lease_expires_at=shard.lease_expires_at,
        lease_seconds_remaining=lease_seconds_remaining,
        lease_status=lease_status,
        failure_category=shard.failure_category,
        error_message=shard.error_message,
    )

def _job_lifecycle_stage(
    *,
    job: Job,
    scan_status: str,
    total_shards: int,
    running_shards: int,
    retrying_shards: int,
    stale_shards: int,
    failed_shards: int,
    stopped_shards: int,
    pending_scan_units: int,
    running_scan_units: int,
    stale_scan_units: int,
    failed_scan_units: int,
) -> str:
    if job.status in TERMINAL_JOB_STATUSES:
        return job.status
    if job.stop_requested or job.status == "stopping":
        return "draining"
    if failed_shards or stopped_shards or failed_scan_units:
        return "failed"
    if retrying_shards or stale_shards or stale_scan_units:
        return "recovering"
    if pending_scan_units or running_scan_units or scan_status == "running":
        return "scanning"
    if scan_status == "done" and total_shards == 0:
        return "sharding"
    if running_shards or total_shards:
        return "running"
    return job.status

def _manifest_snapshot_status(manifest: Manifest | None) -> str:
    if manifest is None:
        return "missing"
    if manifest.frozen_at is not None:
        return "frozen"
    if manifest.status == "scanning":
        return "scanning"
    if manifest.status == "ready":
        return "ready"
    return manifest.status or "unknown"

def _manifest_freeze_integrity_summary(
    manifest: Manifest | None,
    *,
    limits: __ControlLimits | None = None,
) -> dict[str, Any]:
    control_limits = (
        limits if limits is not None else __legacy_control_limits()
    )
    if manifest is None or manifest.frozen_at is None:
        if manifest is not None:
            worker_report = _load_worker_integrity_report(
                manifest,
                limits=control_limits,
            )
            if worker_report is not None:
                worker_summary = _manifest_integrity_freeze_summary(
                    worker_report,
                    limits=control_limits,
                )
                return {
                    "manifest_integrity_status": worker_report.status,
                    "manifest_integrity_ok": worker_report.ok,
                    "manifest_integrity_issue_count": worker_summary["integrity_issue_count"],
                }
        return {
            "manifest_integrity_status": None,
            "manifest_integrity_ok": None,
            "manifest_integrity_issue_count": 0,
        }
    worker_report = _load_worker_integrity_report(
        manifest,
        limits=control_limits,
    )
    if worker_report is not None:
        worker_summary = _manifest_integrity_freeze_summary(
            worker_report,
            limits=control_limits,
        )
        return {
            "manifest_integrity_status": worker_report.status,
            "manifest_integrity_ok": worker_report.ok,
            "manifest_integrity_issue_count": worker_summary["integrity_issue_count"],
        }
    try:
        report = json_loads_object(manifest.freeze_report_json)
    except json.JSONDecodeError:
        return {
            "manifest_integrity_status": "invalid_freeze_report",
            "manifest_integrity_ok": False,
            "manifest_integrity_issue_count": 1,
        }
    return {
        "manifest_integrity_status": report.get("integrity_status"),
        "manifest_integrity_ok": report.get("integrity_ok"),
        "manifest_integrity_issue_count": int(report.get("integrity_issue_count") or 0),
    }

def get_job_summary(
    session: Session,
    job_or_id: Job | str,
    *,
    limits: __ControlLimits | None = None,
) -> JobSummaryResponse:
    control_limits = (
        limits if limits is not None else __legacy_control_limits()
    )
    job = get_job_or_raise(session, job_or_id) if isinstance(job_or_id, str) else job_or_id
    summary_now = utcnow()
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job.id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    file_rows = session.execute(
        select(
            func.count(JobFile.id),
            func.coalesce(
                func.sum(JobFile.status.in_(COMPLETED_FILE_STATUSES).cast(Integer)),
                0,
            ),
            func.coalesce(
                func.sum(JobFile.status.in_(FAILED_FILE_STATUSES).cast(Integer)),
                0,
            ),
            func.coalesce(
                func.sum(JobFile.status.in_(SKIPPED_FILE_STATUSES).cast(Integer)),
                0,
            ),
            func.coalesce(func.sum(JobFile.done_pages), 0),
            func.sum(JobFile.total_pages),
        ).where(JobFile.job_id == job.id)
    ).one()
    observed_total_files = int(file_rows[0] or 0)
    counter = session.get(JobCounter, job.id)
    static_input_files = _static_input_file_count(session, job.id)
    authoritative_total_files = max(observed_total_files, static_input_files)
    counter_total_files = _job_counter_total_files(counter)
    total_files = authoritative_total_files or counter_total_files
    scanned_files = total_files
    completed_files = max(int(file_rows[1] or 0), counter.completed_files if counter else 0)
    failed_files = max(int(file_rows[2] or 0), counter.failed_files if counter else 0)
    skipped_files = max(int(file_rows[3] or 0), counter.skipped_files if counter else 0)
    observed_completed_pages = int(file_rows[4] or 0)
    completed_pages = (
        observed_completed_pages
        if observed_total_files
        else max(observed_completed_pages, counter.completed_pages if counter else 0)
    )
    event_total_pages = counter.total_pages if counter and counter.total_pages > 0 else None
    if file_rows[5] is not None and event_total_pages is not None:
        total_pages = max(int(file_rows[5]), event_total_pages)
    elif file_rows[5] is not None:
        total_pages = int(file_rows[5])
    else:
        total_pages = event_total_pages
    shard_progress_rows = session.execute(
        select(
            func.coalesce(func.sum(WorkShard.processed_files), 0),
            func.coalesce(func.sum(WorkShard.failed_files), 0),
            func.coalesce(func.sum(WorkShard.skipped_files), 0),
            func.coalesce(func.sum(WorkShard.completed_pages), 0),
        ).where(WorkShard.job_id == job.id)
    ).one()
    shard_processed_files = int(shard_progress_rows[0] or 0)
    shard_failed_files = int(shard_progress_rows[1] or 0)
    shard_skipped_files = int(shard_progress_rows[2] or 0)
    shard_completed_files = max(
        shard_processed_files - shard_failed_files - shard_skipped_files,
        0,
    )
    completed_files = max(completed_files, shard_completed_files)
    failed_files = max(failed_files, shard_failed_files)
    skipped_files = max(skipped_files, shard_skipped_files)
    if not observed_total_files:
        completed_pages = max(completed_pages, int(shard_progress_rows[3] or 0))
    if total_files:
        failed_files = min(failed_files, total_files)
        skipped_files = min(skipped_files, max(total_files - failed_files, 0))
        completed_files = min(
            completed_files,
            max(total_files - failed_files - skipped_files, 0),
        )
    if total_pages:
        completed_pages = min(completed_pages, total_pages)
    failure_category_counts = _load_failure_category_counts(counter)
    scan_progress = _latest_manifest_scan_progress(session, job.id)
    scan_progress_files = int(scan_progress.get("scanned_files") or 0)
    scan_progress_dirs = int(scan_progress.get("scanned_dirs") or 0)
    scan_progress_bytes = int(scan_progress.get("total_bytes") or 0)
    scan_error_samples = _recent_manifest_scan_error_samples(session, job.id)
    scan_error_count = int(scan_progress.get("skipped_error_count") or len(scan_error_samples))
    manifest_scan_meta = _manifest_scan_metadata(manifest)
    if manifest is not None and manifest_scan_meta:
        scan_progress_files = max(scan_progress_files, int(manifest.file_count or 0))
        try:
            manifest_scan_dirs = int(
                manifest_scan_meta.get("scanned_dir_count")
                or manifest_scan_meta.get("scanned_dirs")
                or 0
            )
        except (TypeError, ValueError):
            manifest_scan_dirs = 0
        scan_progress_dirs = max(scan_progress_dirs, manifest_scan_dirs)
        scan_progress_bytes = max(scan_progress_bytes, int(manifest.total_bytes or 0))
        try:
            manifest_scan_error_count = int(manifest_scan_meta.get("skipped_error_count") or 0)
        except (TypeError, ValueError):
            manifest_scan_error_count = 0
        scan_error_count = max(scan_error_count, manifest_scan_error_count)
        if not scan_error_samples:
            scan_error_samples = _manifest_scan_error_samples(manifest_scan_meta)
    scanned_files = max(scanned_files, scan_progress_files)

    last_event_at = session.execute(
        select(func.max(JobEvent.created_at)).where(JobEvent.job_id == job.id)
    ).scalar_one()
    if last_event_at is None and counter is not None:
        last_event_at = counter.last_event_at
    first_event_at = session.execute(
        select(func.min(JobEvent.created_at)).where(JobEvent.job_id == job.id)
    ).scalar_one()
    if first_event_at is None and counter is not None:
        first_event_at = counter.first_event_at
    last_heartbeat_at = session.execute(
        select(func.max(JobEvent.created_at))
        .where(JobEvent.job_id == job.id)
        .where(JobEvent.event_type == "job_heartbeat")
    ).scalar_one()
    degraded_pages = int(
        session.execute(
            select(func.count(JobEvent.id))
            .where(JobEvent.job_id == job.id)
            .where(JobEvent.event_type == "page_done")
            .where(JobEvent.status.in_(DEGRADED_PAGE_STATUSES))
        ).scalar_one()
        or 0
    )
    if counter is not None:
        degraded_pages = max(degraded_pages, counter.degraded_pages)
    quality_flags = ["image_fallback"] if degraded_pages > 0 else []
    shard_rows = session.execute(
        select(WorkShard.status, func.count(WorkShard.id))
        .where(WorkShard.job_id == job.id)
        .group_by(WorkShard.status)
    ).all()
    shard_counts = {status: int(count) for status, count in shard_rows}
    total_shards = sum(shard_counts.values())
    shard_failure_category_rows = session.execute(
        select(WorkShard.failure_category, func.count(WorkShard.id))
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.failure_category.is_not(None))
        .group_by(WorkShard.failure_category)
    ).all()
    shard_failure_category_counts = {
        str(category): int(count)
        for category, count in shard_failure_category_rows
        if category
    }
    scan_unit_rows = session.execute(
        select(ScanUnit.status, func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .group_by(ScanUnit.status)
    ).all()
    scan_unit_counts = {status: int(count) for status, count in scan_unit_rows}
    total_scan_units = sum(scan_unit_counts.values())
    scan_unit_failure_category_rows = session.execute(
        select(ScanUnit.failure_category, func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.failure_category.is_not(None))
        .group_by(ScanUnit.failure_category)
    ).all()
    scan_unit_failure_category_counts = {
        str(category): int(count)
        for category, count in scan_unit_failure_category_rows
        if category
    }
    if scan_unit_counts:
        scan_progress_files = max(scan_progress_files, total_files)
        scan_progress_dirs = max(
            scan_progress_dirs,
            scan_unit_counts.get("succeeded", 0) + scan_unit_counts.get("failed", 0),
        )
        manifest_total_bytes = int(
            session.execute(
                select(func.coalesce(func.sum(Manifest.total_bytes), 0)).where(
                    Manifest.job_id == job.id
                )
            ).scalar_one()
            or 0
        )
        scan_progress_bytes = max(scan_progress_bytes, manifest_total_bytes)
        scan_unit_problem_samples = _scan_unit_problem_samples(session, job.id, limit=5)
        if not scan_error_samples:
            scan_error_samples = scan_unit_problem_samples
        scan_error_count = max(
            scan_error_count,
            scan_unit_counts.get("failed", 0),
            scan_unit_counts.get("stale", 0),
        )
    worker_shard_rows = session.execute(
        select(WorkShard.assigned_server_id, WorkShard.status, func.count(WorkShard.id))
        .where(WorkShard.job_id == job.id)
        .group_by(WorkShard.assigned_server_id, WorkShard.status)
    ).all()
    worker_counts: dict[str | None, dict[str, int]] = {}
    for server_id, status, count in worker_shard_rows:
        counts = worker_counts.setdefault(server_id, {})
        counts[status] = int(count)
    attention_shard_priority = case(
        (WorkShard.status == "running", 0),
        (WorkShard.status == "retrying", 1),
        (WorkShard.status == "stale", 2),
        (WorkShard.status == "failed", 3),
        else_=9,
    )
    attention_shard_stmt = (
        select(WorkShard)
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.status.in_(ATTENTION_SHARD_STATUSES))
        .order_by(attention_shard_priority, WorkShard.shard_index.asc())
    )
    if control_limits.job_summary_attention_shard_limit:
        attention_shard_stmt = attention_shard_stmt.limit(
            control_limits.job_summary_attention_shard_limit
        )
    attention_shard_rows = list(session.execute(attention_shard_stmt).scalars().all())
    attention_shard_rows.sort(
        key=lambda shard: (
            {"running": 0, "retrying": 1, "stale": 2, "failed": 3}.get(shard.status, 9),
            shard.shard_index,
        )
    )
    attention_shards = [
        _shard_progress_summary(shard, job, summary_now) for shard in attention_shard_rows
    ]
    current_shards_by_worker: dict[str | None, list[JobShardProgressSummary]] = {}
    for shard_summary in attention_shards:
        if shard_summary.status in CURRENT_WORKER_SHARD_STATUSES:
            current_shards_by_worker.setdefault(shard_summary.assigned_server_id, []).append(shard_summary)
    worker_shards = [
        JobWorkerShardSummary(
            server_id=server_id,
            total_shards=sum(counts.values()),
            pending_shards=counts.get("pending", 0),
            running_shards=counts.get("running", 0),
            retrying_shards=counts.get("retrying", 0),
            stale_shards=counts.get("stale", 0),
            succeeded_shards=counts.get("succeeded", 0),
            failed_shards=counts.get("failed", 0),
            stopped_shards=counts.get("stopped", 0),
            current_shards=current_shards_by_worker.get(server_id, []),
            api_inflight=sum(
                shard.api_inflight for shard in current_shards_by_worker.get(server_id, [])
            ),
            api_inflight_peak=max(
                (shard.api_inflight_peak for shard in current_shards_by_worker.get(server_id, [])),
                default=0,
            ),
            api_waiting=sum(
                shard.api_waiting for shard in current_shards_by_worker.get(server_id, [])
            ),
            oldest_api_inflight=max(
                (
                    shard.oldest_api_inflight
                    for shard in current_shards_by_worker.get(server_id, [])
                ),
                default=0.0,
            ),
            execution_paused=any(
                shard.execution_paused for shard in current_shards_by_worker.get(server_id, [])
            ),
        )
        for server_id, counts in sorted(
            worker_counts.items(),
            key=lambda item: (
                item[0] is None,
                item[0] or "",
            ),
        )
    ]

    progress_percent = None
    if total_pages and total_pages > 0:
        progress_percent = round(min(completed_pages / total_pages * 100, 100), 2)
    elif total_files:
        processed_files = completed_files + failed_files + skipped_files
        progress_percent = round(min(processed_files / total_files * 100, 100), 2)

    started_at = job.started_at or first_event_at or job.created_at
    ended_at = summary_now
    if job.status in TERMINAL_JOB_STATUSES:
        ended_at = job.finished_at or last_event_at or ended_at
    elapsed_seconds = max((ended_at - started_at).total_seconds(), 0.0)
    pages_per_second = None
    files_per_minute = None
    eta_seconds = None
    if elapsed_seconds > 0:
        processed_files = completed_files + failed_files + skipped_files
        if completed_pages > 0:
            pages_per_second = round(completed_pages / elapsed_seconds, 4)
        if processed_files > 0:
            files_per_minute = round(processed_files / elapsed_seconds * 60, 4)
        if pages_per_second and total_pages and completed_pages < total_pages:
            eta_seconds = int((total_pages - completed_pages) / pages_per_second)

    freshness_at = last_heartbeat_at or last_event_at or job.started_at
    is_stale = False
    if job.status in {"running", "stopping"} and freshness_at is not None:
        is_stale = summary_now - freshness_at > timedelta(seconds=STALE_AFTER_SECONDS)

    retrying_shards = shard_counts.get("retrying", 0)
    stale_shards = shard_counts.get("stale", 0)
    failed_shards = shard_counts.get("failed", 0)
    stopped_shards = shard_counts.get("stopped", 0)
    stale_scan_units = scan_unit_counts.get("stale", 0)
    failed_scan_units = scan_unit_counts.get("failed", 0)
    scan_status = "not_started"
    if scan_progress:
        scan_status = str(scan_progress.get("status") or "running")
    if scan_unit_counts:
        open_scan_units = (
            scan_unit_counts.get("pending", 0)
            + scan_unit_counts.get("running", 0)
            + scan_unit_counts.get("stale", 0)
        )
        if open_scan_units:
            scan_status = "running"
        elif failed_scan_units:
            scan_status = "failed"
        else:
            scan_status = "done"
    elif manifest is not None:
        if manifest.status == "scanning":
            scan_status = "running"
        elif manifest.frozen_at is not None or manifest.status == "ready":
            scan_status = "done"
    estimated_total_files_raw = scan_progress.get("estimated_total_files")
    try:
        estimated_total_files = int(estimated_total_files_raw) if estimated_total_files_raw is not None else None
    except (TypeError, ValueError):
        estimated_total_files = None
    scan_remaining_files = _optional_int(scan_progress.get("remaining_files"))
    if scan_remaining_files is None and estimated_total_files is not None:
        scan_remaining_files = max(estimated_total_files - scan_progress_files, 0)
    scan_progress_percent = None
    if estimated_total_files is not None and estimated_total_files > 0:
        scan_progress_percent = round(
            min(scan_progress_files / estimated_total_files * 100, 100),
            2,
        )
    scan_started_at = _parse_datetime(scan_progress.get("scan_started_at"))
    if scan_started_at is None:
        scan_started_at = _parse_datetime(manifest_scan_meta.get("scan_started_at"))
    if scan_started_at is None:
        scan_started_at = _manifest_scan_started_at(session, job.id)
    if scan_started_at is None and scan_unit_counts:
        scan_started_at = session.execute(
            select(func.min(ScanUnit.started_at))
            .where(ScanUnit.job_id == job.id)
            .where(ScanUnit.started_at.is_not(None))
        ).scalar_one()
    if scan_started_at is None and scan_unit_counts:
        scan_started_at = job.started_at or job.created_at
    scan_finished_at = _parse_datetime(scan_progress.get("scan_finished_at"))
    if scan_finished_at is None:
        scan_finished_at = _parse_datetime(manifest_scan_meta.get("scan_finished_at"))
    if scan_finished_at is None and manifest is not None and manifest.frozen_at is not None:
        scan_finished_at = manifest.frozen_at
    recovery_status = "healthy"
    if failed_shards or stopped_shards or failed_scan_units:
        recovery_status = "exhausted"
    elif retrying_shards or stale_shards or stale_scan_units:
        recovery_status = "recovering"
    pending_scan_units = scan_unit_counts.get("pending", 0)
    running_scan_units = scan_unit_counts.get("running", 0)
    succeeded_scan_units = scan_unit_counts.get("succeeded", 0)
    executable_shards = (
        shard_counts.get("pending", 0)
        + shard_counts.get("running", 0)
        + retrying_shards
        + stale_shards
    )
    lifecycle_stage = _job_lifecycle_stage(
        job=job,
        scan_status=scan_status,
        total_shards=total_shards,
        running_shards=shard_counts.get("running", 0),
        retrying_shards=retrying_shards,
        stale_shards=stale_shards,
        failed_shards=failed_shards,
        stopped_shards=stopped_shards,
        pending_scan_units=pending_scan_units,
        running_scan_units=running_scan_units,
        stale_scan_units=stale_scan_units,
        failed_scan_units=failed_scan_units,
    )
    completed_scan_units = succeeded_scan_units + failed_scan_units
    scan_eta_seconds = None
    if scan_status == "running":
        scan_eta_seconds = _optional_int(scan_progress.get("estimated_remaining_seconds"))
        if scan_eta_seconds is None:
            scan_eta_seconds = _scan_eta_seconds_from_rate(
                scanned_files=scan_progress_files,
                estimated_total_files=estimated_total_files,
                files_per_second=scan_progress.get("files_per_second"),
            )
        if scan_eta_seconds is None:
            scan_eta_seconds = _scan_eta_seconds(
                started_at=scan_started_at,
                now=summary_now,
                scanned_files=scan_progress_files,
                estimated_total_files=estimated_total_files,
            )
        if scan_eta_seconds is None:
            scan_eta_seconds = _scan_unit_eta_seconds(
                started_at=scan_started_at,
                now=summary_now,
                completed_units=completed_scan_units,
                total_units=total_scan_units,
            )
    manifest_integrity_summary = _manifest_freeze_integrity_summary(
        manifest,
        limits=control_limits,
    )
    worker_version_summary = _job_worker_version_summary(session, job)

    return JobSummaryResponse(
        id=job.id,
        input_dir=job.input_dir,
        output_dir=job.output_dir,
        engine=job.engine,
        assigned_server_id=public_assigned_server_id(job),
        allowed_server_ids=allowed_server_ids_for_job(job),
        status=job.status,
        lifecycle_stage=lifecycle_stage,
        failure_category=job.failure_category,
        error_message=job.error_message,
        stop_requested=job.stop_requested,
        force_reprocess=job.force_reprocess,
        archived_at=job.archived_at,
        total_files=total_files,
        scanned_files=scanned_files,
        completed_files=completed_files,
        failed_files=failed_files,
        failure_category_counts=failure_category_counts,
        skipped_files=skipped_files,
        total_pages=total_pages,
        completed_pages=completed_pages,
        progress_percent=progress_percent,
        pages_per_second=pages_per_second,
        files_per_minute=files_per_minute,
        eta_seconds=eta_seconds,
        last_event_at=last_event_at,
        last_heartbeat_at=last_heartbeat_at,
        is_stale=is_stale,
        degraded_pages=degraded_pages,
        manifest_status=manifest.status if manifest is not None else None,
        manifest_snapshot_status=_manifest_snapshot_status(manifest),
        manifest_frozen_at=manifest.frozen_at if manifest is not None else None,
        **manifest_integrity_summary,
        scan_status=scan_status,
        scan_progress_files=scan_progress_files,
        scan_discovered_pdf_count=scan_progress_files,
        scan_estimated_total_files=estimated_total_files,
        scan_estimated_total_pdf_count=estimated_total_files,
        scan_remaining_files=scan_remaining_files,
        scan_remaining_pdf_count=scan_remaining_files,
        scan_progress_percent=scan_progress_percent,
        scan_progress_dirs=scan_progress_dirs,
        scan_progress_bytes=scan_progress_bytes,
        scan_current_path=scan_progress.get("current_path"),
        scan_error_count=scan_error_count,
        scan_error_samples=scan_error_samples,
        scan_eta_seconds=scan_eta_seconds,
        scan_started_at=scan_started_at,
        scan_finished_at=scan_finished_at,
        total_shards=total_shards,
        shards_created=total_shards,
        executable_shards=executable_shards,
        pending_shards=shard_counts.get("pending", 0),
        running_shards=shard_counts.get("running", 0),
        retrying_shards=retrying_shards,
        stale_shards=stale_shards,
        succeeded_shards=shard_counts.get("succeeded", 0),
        failed_shards=failed_shards,
        stopped_shards=stopped_shards,
        shard_failure_category_counts=shard_failure_category_counts,
        total_scan_units=total_scan_units,
        pending_scan_units=pending_scan_units,
        running_scan_units=running_scan_units,
        stale_scan_units=stale_scan_units,
        succeeded_scan_units=succeeded_scan_units,
        failed_scan_units=failed_scan_units,
        scan_unit_failure_category_counts=scan_unit_failure_category_counts,
        recovery_status=recovery_status,
        **worker_version_summary,
        worker_shards=worker_shards,
        attention_shards=attention_shards,
        quality_flags=quality_flags,
    )

def list_recent_job_files(session: Session, job_id: str, kind: str, limit: int) -> list[JobFile]:
    get_job_or_raise(session, job_id)
    bounded_limit = max(1, min(limit, 100))
    stmt = select(JobFile).where(JobFile.job_id == job_id)
    if kind == "failed":
        stmt = stmt.where(JobFile.status.in_(FAILED_FILE_STATUSES))
    elif kind == "processed":
        stmt = stmt.where(JobFile.status.in_(PROCESSED_FILE_STATUSES))
    else:
        stmt = stmt.where(JobFile.status.in_(PROCESSED_FILE_STATUSES))
    stmt = stmt.order_by(JobFile.updated_at.desc(), JobFile.id.desc()).limit(bounded_limit)
    rows = list(session.execute(stmt).scalars().all())
    if kind != "failed" or len(rows) >= bounded_limit:
        return rows

    seen_paths = {row.file_path for row in rows}
    counter = session.get(JobCounter, job_id)
    for sample in _load_recent_failed_file_samples(counter):
        file_path = str(sample.get("file_path") or "")
        if not file_path or file_path in seen_paths:
            continue
        rows.append(
            JobFile(
                job_id=job_id,
                file_path=file_path,
                filename=str(sample.get("filename") or file_path.rsplit("/", 1)[-1]),
                status="failed",
                total_pages=_optional_int(sample.get("total_pages")),
                done_pages=_optional_int(sample.get("done_pages")) or 0,
                output_path=sample.get("output_path"),
                error=sample.get("error"),
                failure_category=sample.get("failure_category"),
            )
        )
        seen_paths.add(file_path)
        if len(rows) >= bounded_limit:
            break
    return rows

def _recent_error_from_event(row: JobEvent) -> JobRecentErrorResponse:
    payload = json_loads_object(row.payload_json)
    failure_category = row.failure_category or payload.get("failure_category") or infer_failure_category(payload)
    return JobRecentErrorResponse(
        source="job_event",
        event_type=row.event_type,
        file_path=row.file_path or payload.get("file_path"),
        filename=payload.get("filename"),
        failure_category=str(failure_category) if failure_category else None,
        error=payload.get("error") or payload.get("error_message"),
        created_at=row.created_at,
        payload=payload,
    )

def _recent_error_from_failed_file_sample(sample: dict[str, Any]) -> JobRecentErrorResponse:
    return JobRecentErrorResponse(
        source="failed_file_sample",
        event_type="file_failed",
        file_path=sample.get("file_path"),
        filename=sample.get("filename"),
        failure_category=sample.get("failure_category"),
        error=sample.get("error"),
        created_at=None,
        payload=dict(sample),
    )

def _recent_error_from_event_sample(sample: dict[str, Any]) -> JobRecentErrorResponse:
    payload = sample.get("payload")
    return JobRecentErrorResponse(
        source="event_sample",
        event_type=sample.get("event_type"),
        file_path=sample.get("file_path"),
        filename=sample.get("filename"),
        failure_category=sample.get("failure_category"),
        error=sample.get("error"),
        created_at=_parse_datetime(sample.get("created_at")),
        payload=payload if isinstance(payload, dict) else dict(sample),
    )

def list_recent_job_errors_page(
    session: Session,
    job_id: str,
    *,
    limit: int,
    offset: int,
    failure_category: str | None = None,
) -> JobRecentErrorListResponse:
    get_job_or_raise(session, job_id)
    filters = [JobEvent.job_id == job_id, JobEvent.event_type.in_(PRIORITY_FAILURE_EVENT_TYPES)]
    if failure_category:
        filters.append(JobEvent.failure_category == failure_category)
        total = int(session.execute(select(func.count(JobEvent.id)).where(*filters)).scalar_one())
        event_rows = list(
            session.execute(
                select(JobEvent)
                .where(*filters)
                .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
                .offset(offset)
                .limit(limit)
            ).scalars()
        )
        items = [_recent_error_from_event(row) for row in event_rows]
    else:
        total = int(session.execute(select(func.count(JobEvent.id)).where(*filters)).scalar_one())
        event_rows = list(
            session.execute(
                select(JobEvent)
                .where(*filters)
                .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
                .offset(offset)
                .limit(limit)
            ).scalars()
        )
        items = [_recent_error_from_event(row) for row in event_rows]
    if total == 0:
        event_samples = [
            _recent_error_from_event_sample(sample)
            for sample in _load_recent_error_samples(session.get(JobCounter, job_id))
        ]
        if failure_category:
            event_samples = [
                item for item in event_samples if item.failure_category == failure_category
            ]
        samples = [
            _recent_error_from_failed_file_sample(sample)
            for sample in _load_recent_failed_file_samples(session.get(JobCounter, job_id))
        ]
        if failure_category:
            samples = [
                item for item in samples if item.failure_category == failure_category
            ]
        fallback_items = event_samples + samples
        total = len(fallback_items)
        items = fallback_items[offset : offset + limit]
    return JobRecentErrorListResponse(
        job_id=job_id,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )

# Transitional domain integration names. Manifest projections still consume
# these helpers until their ownership moves in PR 7b.
latest_manifest_scan_progress = _latest_manifest_scan_progress
manifest_scan_error_samples = _manifest_scan_error_samples
manifest_scan_metadata = _manifest_scan_metadata
recent_manifest_scan_error_samples = _recent_manifest_scan_error_samples
scan_unit_problem_samples = _scan_unit_problem_samples

__all__ = [name for name in globals() if not name.startswith("__")]
