from __future__ import annotations

import json
import math
import os
import posixpath
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ocr_parser.infra.failure_category import infer_failure_category
from ocr_parser.config import ParserConfig
from ocr_platform.manifest.models import ManifestItem
from ocr_platform.manifest.scanner import scan_folder_snapshot
from ocr_platform.manifest.sharder import write_manifest_snapshot
from sqlalchemy import Integer, case, delete, distinct, func, select, update
from sqlalchemy.orm import Session

from ... import database
from ...models import Job, JobCounter, JobEvent, JobFile, JobLog, Manifest, ModelProfile, ScanUnit, Server, ShardAttempt, WorkShard
from ...schemas import (
    JobCreateRequest, JobEventRequest, JobLogListResponse, JobLogRequest, JobLogResponse,
    ManifestFreezeReportResponse, ManifestIntegrityResponse, ManifestIntegrityWorkerCompleteRequest,
    ManifestIntegrityWorkerRequestResponse, ManifestIntegrityWorkerTask, ManifestIntegrityWorkerShardTask,
    ManifestIntegrityScanUnitIssue, ManifestIntegrityShardIssue, JobPreflightIssue, JobPreflightResponse,
    JobRecentErrorListResponse, JobRecentErrorResponse, JobSummaryListResponse, JobShardProgressSummary,
    JobSummaryResponse, JobWorkerShardSummary, ModelProfileRequest, ModelProfileResponse,
    ScanUnitCompleteRequest, ScanUnitFailRequest, ServerHeartbeatRequest, ServerRegisterRequest,
    ShardAttemptListResponse, WorkShardUpdateRequest, RemoteManifestRegisterRequest, ShardAttemptResponse,
)
from ..common import *

def _create_distributed_scan_for_job(*args, **kwargs):
    from ..manifests.core import _create_distributed_scan_for_job as target
    return target(*args, **kwargs)

def _create_static_shards_for_job(*args, **kwargs):
    from ..manifests.core import _create_static_shards_for_job as target
    return target(*args, **kwargs)

def _database_migration_preflight_issue(*args, **kwargs):
    from ..workers.core import _database_migration_preflight_issue as target
    return target(*args, **kwargs)

def _effective_job_model_config(*args, **kwargs):
    from ..model_profiles.core import _effective_job_model_config as target
    return target(*args, **kwargs)

def _job_worker_version_summary(*args, **kwargs):
    from ..workers.core import _job_worker_version_summary as target
    return target(*args, **kwargs)

def _load_worker_integrity_report(*args, **kwargs):
    from ..manifests.core import _load_worker_integrity_report as target
    return target(*args, **kwargs)

def _manifest_integrity_freeze_summary(*args, **kwargs):
    from ..manifests.core import _manifest_integrity_freeze_summary as target
    return target(*args, **kwargs)

def allowed_server_ids_for_job(*args, **kwargs):
    from ..workers.core import allowed_server_ids_for_job as target
    return target(*args, **kwargs)

def ensure_pool_server(*args, **kwargs):
    from ..workers.core import ensure_pool_server as target
    return target(*args, **kwargs)

def finalize_stopped_job_if_idle(*args, **kwargs):
    from ..manifests.core import finalize_stopped_job_if_idle as target
    return target(*args, **kwargs)

def has_static_shards(*args, **kwargs):
    from ..manifests.core import has_static_shards as target
    return target(*args, **kwargs)

def infer_default_manifest_root(*args, **kwargs):
    from ..manifests.core import infer_default_manifest_root as target
    return target(*args, **kwargs)

def public_assigned_server_id(*args, **kwargs):
    from ..workers.core import public_assigned_server_id as target
    return target(*args, **kwargs)

def reconcile_expired_scan_unit_leases(*args, **kwargs):
    from ..workers.core import reconcile_expired_scan_unit_leases as target
    return target(*args, **kwargs)

def reconcile_expired_shard_leases(*args, **kwargs):
    from ..workers.core import reconcile_expired_shard_leases as target
    return target(*args, **kwargs)

def stop_reclaimable_work_for_job(*args, **kwargs):
    from ..manifests.core import stop_reclaimable_work_for_job as target
    return target(*args, **kwargs)


def create_job(session: Session, request: JobCreateRequest) -> Job:
    if request.input_mode not in ALLOWED_INPUT_MODES:
        raise ValueError(f"unknown input_mode: {request.input_mode}")
    migration_issue = _database_migration_preflight_issue(
        database.describe_database_status(session.get_bind())
    )
    if migration_issue is not None:
        raise ValueError(migration_issue.message)
    model_config = _effective_job_model_config(session, request)
    assigned_server_id = request.assigned_server_id
    if request.input_mode == "directory" and not assigned_server_id:
        raise ValueError("assigned_server_id is required for directory input_mode")
    if request.input_mode != "directory" and not assigned_server_id:
        assigned_server_id = ensure_pool_server(session).id
    assigned_server = session.get(Server, assigned_server_id) if assigned_server_id is not None else None
    if assigned_server is None or assigned_server.archived_at is not None:
        raise ValueError(f"unknown assigned server: {assigned_server_id}")
    allowed_server_ids = list(dict.fromkeys(request.allowed_server_ids))
    for server_id in allowed_server_ids:
        server = session.get(Server, server_id)
        if server is None or server.archived_at is not None or server_id == POOL_SERVER_ID:
            raise ValueError(f"unknown allowed server: {server_id}")
    manifest_root = request.manifest_root or infer_default_manifest_root(
        session,
        input_dir=request.input_dir,
        input_mode=request.input_mode,
        assigned_server_id=assigned_server_id,
        allowed_server_ids=allowed_server_ids,
    )

    job = Job(
        input_dir=request.input_dir,
        output_dir=request.output_dir,
        engine=model_config["engine"],
        input_mode=request.input_mode,
        model_profile_id=request.model_profile_id,
        manifest_root=manifest_root,
        target_files_per_shard=request.target_files_per_shard,
        max_shard_attempts=request.max_shard_attempts,
        assigned_server_id=assigned_server_id,
        allowed_server_ids_json=json_dumps(allowed_server_ids),
        engine_config=request.engine_config,
        ip=model_config["ip"],
        port=model_config["port"],
        model_name=model_config["model_name"],
        page_concurrency=model_config["page_concurrency"],
        force_reprocess=request.force_reprocess,
        extra_args_json=json_dumps(model_config["extra_args"]),
    )
    session.add(job)
    try:
        session.flush()
        _create_static_shards_for_job(session, job, request)
        _create_distributed_scan_for_job(session, job)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(job)
    return job

def _normalized_status_filter(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = status.strip().lower()
    if not normalized or normalized == "all":
        return None
    if normalized not in JOB_STATUS_FILTERS:
        allowed = ", ".join(sorted(JOB_STATUS_FILTERS))
        raise ValueError(
            f"unknown job status filter: {status}; allowed values: all, {allowed}"
        )
    return normalized

def list_job_summaries(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
) -> list[JobSummaryResponse]:
    stmt = select(Job).order_by(Job.created_at.desc())
    status = _normalized_status_filter(status)
    if status:
        stmt = stmt.where(Job.status == status)
    if not include_archived:
        stmt = stmt.where(Job.archived_at.is_(None))
    stmt = stmt.offset(max(offset, 0)).limit(max(limit, 1))
    jobs = session.execute(stmt).scalars().all()
    return [get_job_summary(session, job) for job in jobs]

def list_job_summaries_page(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
) -> JobSummaryListResponse:
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
        items=[get_job_summary(session, job) for job in jobs],
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

def _manifest_scan_metadata(manifest: Manifest | None) -> dict[str, Any]:
    if manifest is None or not manifest.meta_path:
        return {}
    try:
        payload = json.loads(Path(manifest.meta_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}

def _manifest_scan_error_samples(manifest_meta: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for item in manifest_meta.get("skipped_errors") or []:
        if not isinstance(item, dict):
            continue
        samples.append(_scan_error_sample_with_category(item))
        if len(samples) >= limit:
            break
    return samples

def _recent_manifest_scan_error_samples(session: Session, job_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
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
            key = (str(sample.get("path") or ""), str(sample.get("reason") or ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            samples.append(sample)
            if len(samples) >= limit:
                return samples
    return samples

def _scan_unit_problem_samples(session: Session, job_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "reason": error_message or f"scan unit {status}",
            "failure_category": failure_category,
        }
        for path, status, error_message, failure_category in session.execute(
            select(ScanUnit.path, ScanUnit.status, ScanUnit.error_message, ScanUnit.failure_category)
            .where(ScanUnit.job_id == job_id)
            .where(ScanUnit.status.in_({"failed", "stale"}))
            .order_by(ScanUnit.id.asc())
            .limit(limit)
        ).all()
    ]

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

def _manifest_freeze_integrity_summary(manifest: Manifest | None) -> dict[str, Any]:
    if manifest is None or manifest.frozen_at is None:
        if manifest is not None:
            worker_report = _load_worker_integrity_report(manifest)
            if worker_report is not None:
                worker_summary = _manifest_integrity_freeze_summary(worker_report)
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
    worker_report = _load_worker_integrity_report(manifest)
    if worker_report is not None:
        worker_summary = _manifest_integrity_freeze_summary(worker_report)
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

def get_job_summary(session: Session, job_or_id: Job | str) -> JobSummaryResponse:
    job = get_job_or_raise(session, job_or_id) if isinstance(job_or_id, str) else job_or_id
    reconcile_expired_shard_leases(session, job_id=job.id)
    reconcile_expired_scan_unit_leases(session, job_id=job.id)
    session.flush()
    finalize_stopped_job_if_idle(session, job)
    session.flush()
    session.commit()
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
    if JOB_SUMMARY_ATTENTION_SHARD_LIMIT:
        attention_shard_stmt = attention_shard_stmt.limit(JOB_SUMMARY_ATTENTION_SHARD_LIMIT)
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
    manifest_integrity_summary = _manifest_freeze_integrity_summary(manifest)
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

def request_stop(session: Session, job_id: str) -> Job:
    job = get_job_or_raise(session, job_id)
    job.stop_requested = True
    if job.status == "queued":
        job.status = "stopped"
        if job.finished_at is None:
            job.finished_at = utcnow()
    elif job.status == "running":
        job.status = "stopping"
    stop_reclaimable_work_for_job(session, job)
    finalize_stopped_job_if_idle(session, job)
    session.commit()
    session.refresh(job)
    return job

def delete_job(session: Session, job_id: str) -> None:
    job = get_job_or_raise(session, job_id)
    if job.status not in TERMINAL_JOB_STATUSES:
        raise JobNotTerminalError(f"job is not terminal: {job_id}")

    session.delete(job)
    session.commit()

def archive_job(session: Session, job_id: str) -> Job:
    job = get_job_or_raise(session, job_id)
    if job.status not in TERMINAL_JOB_STATUSES:
        raise JobNotTerminalError(f"job is not terminal: {job_id}")
    if job.archived_at is None:
        job.archived_at = utcnow()
        session.commit()
        session.refresh(job)
    return job

def get_job_or_raise(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise UnknownJobError(f"unknown job: {job_id}")
    return job

def parse_page_no(payload: dict[str, Any]) -> int | None:
    page_no = payload.get("page_no")
    if page_no is None:
        return None
    return int(page_no)

def upsert_job_file_from_event(session: Session, job: Job, event: JobEventRequest) -> None:
    payload = event.payload
    file_path = payload.get("file_path")
    if not file_path:
        return

    filename = payload.get("filename") or file_path.rsplit("/", 1)[-1]
    stmt = select(JobFile).where(JobFile.job_id == job.id).where(JobFile.file_path == file_path)
    job_file = session.execute(stmt).scalar_one_or_none()
    if job_file is None:
        job_file = JobFile(
            job_id=job.id,
            file_path=file_path,
            filename=filename,
            status="pending",
            done_pages=0,
        )
        session.add(job_file)

    if event.type == "file_started":
        job_file.status = "running"
        job_file.error = None
        job_file.failure_category = None
        if payload.get("total_pages") is not None:
            job_file.total_pages = int(payload["total_pages"])
    elif event.type == "page_done":
        page_no = parse_page_no(payload)
        if page_no is not None:
            done_pages_stmt = (
                select(func.count(distinct(JobEvent.page_no)))
                .where(JobEvent.job_id == job.id)
                .where(JobEvent.file_path == file_path)
                .where(JobEvent.event_type == "page_done")
                .where(JobEvent.page_no.is_not(None))
            )
            job_file.done_pages = int(session.execute(done_pages_stmt).scalar_one())
        job_file.status = "running"
        if payload.get("status") in {"error", "failed"}:
            job_file.status = "failed"
            job_file.error = payload.get("error")
            job_file.failure_category = infer_failure_category(payload)
    elif event.type == "file_done":
        job_file.status = payload.get("status") or "success"
        job_file.output_path = payload.get("output_path")
        job_file.error = None
        job_file.failure_category = None
    elif event.type == "file_failed":
        job_file.status = "failed"
        job_file.error = payload.get("error")
        job_file.failure_category = infer_failure_category(payload)

def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def get_or_create_job_counter(session: Session, job_id: str) -> JobCounter:
    counter = session.get(JobCounter, job_id)
    if counter is None:
        counter = JobCounter(job_id=job_id)
        session.add(counter)
        session.flush()
    return counter

def _load_recent_failed_file_samples(counter: JobCounter | None) -> list[dict[str, Any]]:
    if counter is None:
        return []
    try:
        value = json.loads(counter.recent_failed_files_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    samples: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("file_path") or "")
        if not file_path:
            continue
        samples.append(item)
    return samples

def _load_recent_error_samples(counter: JobCounter | None) -> list[dict[str, Any]]:
    if counter is None:
        return []
    try:
        value = json.loads(counter.recent_errors_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    samples: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if not item.get("event_type"):
            continue
        samples.append(item)
    return samples

def _load_failure_category_counts(counter: JobCounter | None) -> dict[str, int]:
    if counter is None:
        return {}
    try:
        value = json.loads(counter.failure_category_counts_json or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, count in value.items():
        try:
            numeric_count = int(count)
        except (TypeError, ValueError):
            continue
        if numeric_count > 0:
            counts[str(key)] = numeric_count
    return counts

def _increment_failure_category_count(counter: JobCounter, category: str) -> None:
    counts = _load_failure_category_counts(counter)
    counts[category] = counts.get(category, 0) + 1
    counter.failure_category_counts_json = json_dumps(
        dict(sorted(counts.items(), key=lambda item: item[0]))
    )

def _failed_file_sample_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    file_path = payload.get("file_path")
    if not file_path:
        return None
    file_path = str(file_path)
    filename = payload.get("filename") or file_path.rsplit("/", 1)[-1]
    return {
        "file_path": file_path,
        "filename": str(filename),
        "status": "failed",
        "total_pages": _optional_int(payload.get("total_pages")),
        "done_pages": _optional_int(payload.get("done_pages")) or 0,
        "output_path": payload.get("output_path"),
        "error": payload.get("error"),
        "failure_category": infer_failure_category(payload),
    }

def _store_recent_failed_file_sample(counter: JobCounter, payload: dict[str, Any]) -> None:
    limit = max(0, JOB_FAILED_FILE_SAMPLE_LIMIT)
    sample = _failed_file_sample_from_payload(payload)
    if sample is None:
        return
    if limit == 0:
        counter.recent_failed_files_json = "[]"
        return

    file_path = sample["file_path"]
    samples = [
        item
        for item in _load_recent_failed_file_samples(counter)
        if str(item.get("file_path") or "") != file_path
    ]
    samples.insert(0, sample)
    counter.recent_failed_files_json = json_dumps(samples[:limit])

def _failure_event_sample_from_event(
    event: JobEventRequest,
    *,
    event_time: datetime,
) -> dict[str, Any] | None:
    if event.type not in PRIORITY_FAILURE_EVENT_TYPES or event.type == "file_failed":
        return None
    payload = event.payload
    return {
        "event_type": event.type,
        "file_path": payload.get("file_path"),
        "filename": payload.get("filename"),
        "failure_category": infer_failure_category(payload),
        "error": payload.get("error") or payload.get("error_message"),
        "created_at": event_time.isoformat(),
        "payload": payload,
    }

def _store_recent_error_sample(
    counter: JobCounter,
    event: JobEventRequest,
    *,
    event_time: datetime,
) -> None:
    limit = max(0, JOB_RECENT_ERROR_SAMPLE_LIMIT)
    sample = _failure_event_sample_from_event(event, event_time=event_time)
    if sample is None:
        return
    if limit == 0:
        counter.recent_errors_json = "[]"
        return

    samples = _load_recent_error_samples(counter)
    samples.insert(0, sample)
    counter.recent_errors_json = json_dumps(samples[:limit])

def job_counter_event_already_seen(session: Session, job_id: str, event: JobEventRequest) -> bool:
    payload = event.payload
    file_path = payload.get("file_path")
    if not file_path:
        return False
    stmt = (
        select(JobEvent.id)
        .where(JobEvent.job_id == job_id)
        .where(JobEvent.event_type == event.type)
        .where(JobEvent.file_path == file_path)
    )
    if event.type == "page_done":
        page_no = parse_page_no(payload)
        if page_no is None:
            return False
        stmt = stmt.where(JobEvent.page_no == page_no)
    elif event.type not in {"file_started", "file_done", "file_failed"}:
        return False
    return session.execute(stmt.limit(1)).scalar_one_or_none() is not None

def update_job_counter_from_event(
    session: Session,
    job: Job,
    event: JobEventRequest,
    *,
    event_time: datetime,
) -> JobCounter:
    counter = get_or_create_job_counter(session, job.id)
    if counter.first_event_at is None:
        counter.first_event_at = event_time
    counter.last_event_at = event_time

    payload = event.payload
    if job_counter_event_already_seen(session, job.id, event):
        return counter
    if event.type == "file_started":
        counter.started_files += 1
        if payload.get("total_pages") is not None:
            try:
                counter.total_pages += int(payload["total_pages"])
            except (TypeError, ValueError):
                pass
    elif event.type == "page_done":
        counter.completed_pages += 1
        if payload.get("status") in DEGRADED_PAGE_STATUSES:
            counter.degraded_pages += 1
    elif event.type == "file_done":
        if payload.get("status") == "skipped":
            counter.skipped_files += 1
        else:
            counter.completed_files += 1
    elif event.type == "file_failed":
        counter.failed_files += 1
        _increment_failure_category_count(counter, infer_failure_category(payload))
        _store_recent_failed_file_sample(counter, payload)
    if event.type in PRIORITY_FAILURE_EVENT_TYPES:
        _store_recent_error_sample(counter, event, event_time=event_time)
    return counter

def _job_counter_total_files(counter: JobCounter | None) -> int:
    if counter is None:
        return 0
    terminal_files = counter.completed_files + counter.failed_files + counter.skipped_files
    return max(counter.started_files, terminal_files)

def prune_job_detail_rows(session: Session, job_id: str) -> None:
    if JOB_FILE_DETAIL_LIMIT >= 0:
        if JOB_FILE_DETAIL_LIMIT == 0:
            stale_file_ids = list(
                session.execute(
                    select(JobFile.id).where(JobFile.job_id == job_id)
                ).scalars()
            )
        else:
            recent_file_ids = list(
                session.execute(
                    select(JobFile.id)
                    .where(JobFile.job_id == job_id)
                    .order_by(
                        case((JobFile.status.in_(FAILED_FILE_STATUSES), 0), else_=1),
                        JobFile.updated_at.desc(),
                        JobFile.id.desc(),
                    )
                    .limit(JOB_FILE_DETAIL_LIMIT + 1)
                ).scalars()
            )
            stale_file_ids = recent_file_ids[JOB_FILE_DETAIL_LIMIT:]
        if stale_file_ids:
            session.execute(delete(JobFile).where(JobFile.id.in_(stale_file_ids)))
    if JOB_EVENT_DETAIL_LIMIT >= 0:
        if JOB_EVENT_DETAIL_LIMIT == 0:
            stale_event_ids = list(
                session.execute(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id)
                    .where(
                        JobEvent.event_type.not_in(
                            RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED
                        )
                    )
                ).scalars()
            )
        else:
            recent_event_ids = list(
                session.execute(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id)
                    .order_by(
                        case((JobEvent.event_type.in_(PRIORITY_FAILURE_EVENT_TYPES), 0), else_=1),
                        case((JobEvent.event_type.in_(PRIORITY_TERMINAL_EVENT_TYPES), 0), else_=1),
                        JobEvent.created_at.desc(),
                        JobEvent.id.desc(),
                    )
                    .limit(JOB_EVENT_DETAIL_LIMIT + 1)
                ).scalars()
            )
            stale_event_ids = recent_event_ids[JOB_EVENT_DETAIL_LIMIT:]
        if stale_event_ids:
            session.execute(delete(JobEvent).where(JobEvent.id.in_(stale_event_ids)))
    if JOB_EVENT_DETAIL_LIMIT == 0 and RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED >= 0:
        for event_type in RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED:
            retained_event_ids = list(
                session.execute(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id)
                    .where(JobEvent.event_type == event_type)
                    .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
                    .limit(RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED + 1)
                ).scalars()
            )
            stale_retained_event_ids = retained_event_ids[
                RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED:
            ]
            if stale_retained_event_ids:
                session.execute(
                    delete(JobEvent).where(JobEvent.id.in_(stale_retained_event_ids))
                )

def record_event(session: Session, job_id: str, event: JobEventRequest) -> Job:
    job = get_job_or_raise(session, job_id)
    payload = event.payload
    page_no = parse_page_no(payload)
    event_time = utcnow()
    update_job_counter_from_event(session, job, event, event_time=event_time)
    if (
        JOB_EVENT_DETAIL_LIMIT != 0
        or event.type in RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED
    ):
        failure_category = (
            infer_failure_category(payload)
            if event.type in PRIORITY_FAILURE_EVENT_TYPES
            else None
        )
        row = JobEvent(
            job_id=job.id,
            event_type=event.type,
            file_path=payload.get("file_path"),
            page_no=page_no,
            status=payload.get("status"),
            failure_category=failure_category,
            payload_json=json_dumps(payload),
            created_at=event_time,
        )
        session.add(row)
        session.flush()
    if JOB_FILE_DETAIL_LIMIT != 0:
        upsert_job_file_from_event(session, job, event)
    session.flush()
    prune_job_detail_rows(session, job.id)

    terminal_status = TERMINAL_EVENT_STATUSES.get(event.type)
    is_static_child_terminal = (
        terminal_status is not None
        and has_static_shards(session, job.id)
        and not payload.get("static_shards_final")
    )
    stop_active = job.status == "stopping" or (
        job.stop_requested and job.status not in TERMINAL_JOB_STATUSES
    )
    if terminal_status is not None and not is_static_child_terminal:
        if stop_active:
            job.status = "stopped"
            if job.failure_category is None:
                job.failure_category = "operator_stopped"
            if job.finished_at is None:
                job.finished_at = utcnow()
        elif job.status not in TERMINAL_JOB_STATUSES:
            job.status = terminal_status
            if event.type == "job_failed":
                job.failure_category = infer_failure_category(payload)
                job.error_message = payload.get("error") or payload.get("error_message")
            job.finished_at = utcnow()

    session.commit()
    session.refresh(job)
    return job

def record_log(session: Session, job_id: str, request: JobLogRequest) -> JobLog:
    get_job_or_raise(session, job_id)
    if JOB_LOG_DETAIL_LIMIT == 0:
        session.commit()
        return JobLog(
            job_id=job_id,
            server_id=request.server_id,
            stream=request.stream,
            line=request.line,
        )
    row = JobLog(
        job_id=job_id,
        server_id=request.server_id,
        stream=request.stream,
        line=request.line,
    )
    session.add(row)
    session.flush()
    if JOB_LOG_DETAIL_LIMIT >= 0:
        recent_log_ids = list(
            session.execute(
                select(JobLog.id)
                .where(JobLog.job_id == job_id)
                .order_by(JobLog.created_at.desc(), JobLog.id.desc())
                .limit(JOB_LOG_DETAIL_LIMIT + 1)
            ).scalars()
        )
        stale_log_ids = recent_log_ids[JOB_LOG_DETAIL_LIMIT:]
        if stale_log_ids:
            session.execute(delete(JobLog).where(JobLog.id.in_(stale_log_ids)))
    session.commit()
    session.refresh(row)
    return row

def job_log_to_response(row: JobLog) -> JobLogResponse:
    return JobLogResponse(
        id=row.id,
        job_id=row.job_id,
        server_id=row.server_id,
        stream=row.stream,
        line=row.line,
        created_at=row.created_at,
    )

def list_job_logs_page(
    session: Session,
    job_id: str,
    *,
    limit: int,
    offset: int,
    server_id: str | None = None,
    stream: str | None = None,
) -> JobLogListResponse:
    get_job_or_raise(session, job_id)
    filters = [JobLog.job_id == job_id]
    if server_id:
        filters.append(JobLog.server_id == server_id)
    if stream:
        filters.append(JobLog.stream == stream)
    total = int(
        session.execute(select(func.count(JobLog.id)).where(*filters)).scalar_one()
    )
    rows = list(
        session.execute(
            select(JobLog)
            .where(*filters)
            .order_by(JobLog.created_at.desc(), JobLog.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return JobLogListResponse(
        job_id=job_id,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
        items=[job_log_to_response(row) for row in rows],
    )

__all__ = [name for name in globals() if not name.startswith("__")]
