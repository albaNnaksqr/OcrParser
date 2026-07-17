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

def _latest_manifest_scan_progress(*args, **kwargs):
    from ..jobs.core import _latest_manifest_scan_progress as target
    return target(*args, **kwargs)

def _manifest_scan_error_samples(*args, **kwargs):
    from ..jobs.core import _manifest_scan_error_samples as target
    return target(*args, **kwargs)

def _manifest_scan_metadata(*args, **kwargs):
    from ..jobs.core import _manifest_scan_metadata as target
    return target(*args, **kwargs)

def _normal_posix_path(*args, **kwargs):
    from ..workers.core import _normal_posix_path as target
    return target(*args, **kwargs)

def _path_is_under(*args, **kwargs):
    from ..workers.core import _path_is_under as target
    return target(*args, **kwargs)

def _recent_manifest_scan_error_samples(*args, **kwargs):
    from ..jobs.core import _recent_manifest_scan_error_samples as target
    return target(*args, **kwargs)

def _remaining_retry_status(*args, **kwargs):
    from ..workers.core import _remaining_retry_status as target
    return target(*args, **kwargs)

def _scan_unit_problem_samples(*args, **kwargs):
    from ..jobs.core import _scan_unit_problem_samples as target
    return target(*args, **kwargs)

def evaluate_server_path_access(*args, **kwargs):
    from ..workers.core import evaluate_server_path_access as target
    return target(*args, **kwargs)

def get_job_or_raise(*args, **kwargs):
    from ..jobs.core import get_job_or_raise as target
    return target(*args, **kwargs)

def list_servers(*args, **kwargs):
    from ..workers.core import list_servers as target
    return target(*args, **kwargs)

def reconcile_expired_scan_unit_leases(*args, **kwargs):
    from ..workers.core import reconcile_expired_scan_unit_leases as target
    return target(*args, **kwargs)

def reconcile_expired_shard_leases(*args, **kwargs):
    from ..workers.core import reconcile_expired_shard_leases as target
    return target(*args, **kwargs)

def scan_unit_lease_deadline(*args, **kwargs):
    from ..workers.core import scan_unit_lease_deadline as target
    return target(*args, **kwargs)

def server_is_allowed_for_job(*args, **kwargs):
    from ..workers.core import server_is_allowed_for_job as target
    return target(*args, **kwargs)

def shard_lease_deadline(*args, **kwargs):
    from ..workers.core import shard_lease_deadline as target
    return target(*args, **kwargs)


def default_manifest_root_for_shared_path(shared_root: str) -> str:
    normalized_root = _normal_posix_path(shared_root).rstrip("/") or "/"
    return posixpath.join(normalized_root, DEFAULT_MANIFEST_ROOT_SUFFIX)

def infer_default_manifest_root(
    session: Session,
    *,
    input_dir: str,
    input_mode: str,
    assigned_server_id: str | None,
    allowed_server_ids: list[str],
) -> str | None:
    if input_mode == "existing_manifest":
        return None
    if input_mode == "directory":
        scoped_server_ids = [assigned_server_id] if assigned_server_id else []
    else:
        scoped_server_ids = allowed_server_ids

    if scoped_server_ids:
        servers = [
            server
            for server_id in scoped_server_ids
            if server_id and server_id != POOL_SERVER_ID
            for server in [session.get(Server, server_id)]
            if server is not None and server.archived_at is None
        ]
    else:
        servers = [server for server in list_servers(session) if server.id != POOL_SERVER_ID]

    matched_roots = {
        item["matched_path"]
        for server in servers
        for item in [evaluate_server_path_access(server, input_dir)]
        if item["can_access"] and item["matched_path"]
    }
    if len(matched_roots) != 1:
        return None
    return default_manifest_root_for_shared_path(next(iter(matched_roots)))

def server_can_access_input_dir(session: Session, server_id: str, input_dir: str) -> bool:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return False
    return bool(evaluate_server_path_access(server, input_dir)["can_access"])

def _manifest_output_dir(job: Job, request: JobCreateRequest) -> Path:
    manifest_root = request.manifest_root or job.manifest_root
    if manifest_root:
        return Path(manifest_root) / job.id
    return Path(job.output_dir) / "_manifest" / job.id

def _manifest_output_dir_for_job(job: Job) -> Path:
    if job.manifest_root:
        return Path(job.manifest_root) / job.id
    return Path(job.output_dir) / "_manifest" / job.id

def _read_manifest_items(manifest_path: Path) -> list[ManifestItem]:
    items: list[ManifestItem] = []
    seen_relative_paths: set[str] = set()
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = ManifestItem.from_json_line(line)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"malformed manifest row in {manifest_path} at line {line_number}: {exc}"
            ) from exc
        relative_path = Path(item.relative_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(
                f"malformed manifest row in {manifest_path} at line {line_number}: "
                f"relative_path must be relative and may not contain '..': {item.relative_path}"
            )
        relative_key = relative_path.as_posix()
        if relative_key in seen_relative_paths:
            raise ValueError(
                f"malformed manifest row in {manifest_path}: duplicate relative_path "
                f"would overwrite output: {relative_key} line {line_number}"
            )
        seen_relative_paths.add(relative_key)
        items.append(item)
    return items

def _create_static_shards_for_job(session: Session, job: Job, request: JobCreateRequest) -> None:
    if request.input_mode not in ALLOWED_INPUT_MODES:
        raise ValueError(f"unknown input_mode: {request.input_mode}")
    if request.input_mode not in CONTROL_STATIC_INPUT_MODES:
        return

    if request.input_mode == "folder_snapshot":
        scan = scan_folder_snapshot(request.input_dir)
        items = scan.items
        input_root = scan.input_root
        skipped_errors = scan.skipped_errors
        skipped_error_count = scan.scan_error_count
        scanned_dir_count = scan.scanned_dir_count
    else:
        if not request.manifest_path:
            raise ValueError("manifest_path is required for existing_manifest input_mode")
        manifest_file = Path(request.manifest_path)
        if not manifest_file.exists():
            raise FileNotFoundError(f"manifest file not found: {manifest_file}")
        items = _read_manifest_items(manifest_file)
        input_root = request.input_dir
        skipped_errors = None
        skipped_error_count = None
        scanned_dir_count = None

    written = write_manifest_snapshot(
        job_id=job.id,
        input_root=input_root,
        output_dir=_manifest_output_dir(job, request),
        items=items,
        target_files_per_shard=request.target_files_per_shard,
        input_mode=request.input_mode,
        skipped_errors=skipped_errors,
        skipped_error_count=skipped_error_count,
        scanned_dir_count=scanned_dir_count,
    )
    manifest = Manifest(
        job_id=job.id,
        input_mode=request.input_mode,
        input_root=input_root,
        manifest_path=str(written.manifest_path),
        meta_path=str(written.meta_path),
        file_count=written.file_count,
        total_bytes=written.total_bytes,
        next_shard_index=len(written.shards) + 1,
        status="ready",
    )
    session.add(manifest)
    session.flush()

    for shard in written.shards:
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=shard.index,
                shard_path=str(shard.path),
                status="pending",
                file_count=shard.file_count,
            )
        )
    session.flush()
    freeze_manifest_if_scan_complete(session, job, manifest)

def _create_distributed_scan_for_job(session: Session, job: Job) -> None:
    if job.input_mode not in REMOTE_DISTRIBUTED_SCAN_INPUT_MODES:
        return
    root = _manifest_output_dir_for_job(job)
    session.add(
        Manifest(
            job_id=job.id,
            input_mode=job.input_mode,
            input_root=job.input_dir,
            manifest_path=str(root / "manifest.jsonl"),
            meta_path=str(root / "manifest.meta.json"),
            file_count=0,
            total_bytes=0,
            status="scanning",
        )
    )
    session.add(ScanUnit(job_id=job.id, path=job.input_dir, status="pending"))

def register_remote_manifest(
    session: Session,
    job_id: str,
    request: RemoteManifestRegisterRequest,
) -> Manifest:
    # Lock the Job row first so concurrent registrations for the same job are
    # serialised — the two guard SELECTs below are otherwise a TOCTOU race.
    job = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
    if job is None:
        raise UnknownJobError(f"unknown job: {job_id}")
    if job.input_mode not in REMOTE_STATIC_INPUT_MODES:
        raise ValueError(f"job input_mode does not accept remote manifest registration: {job.input_mode}")
    if has_static_shards(session, job.id):
        raise ValueError(f"job already has registered shards: {job.id}")
    if session.execute(select(func.count(Manifest.id)).where(Manifest.job_id == job.id)).scalar_one():
        raise ValueError(f"job already has registered manifest: {job.id}")
    manifest_file = Path(request.manifest_path)
    if manifest_file.exists():
        _read_manifest_items(manifest_file)

    manifest = Manifest(
        job_id=job.id,
        input_mode=request.input_mode,
        input_root=request.input_root,
        manifest_path=request.manifest_path,
        meta_path=request.meta_path,
        file_count=request.file_count,
        total_bytes=request.total_bytes,
        next_shard_index=len(request.shards) + 1,
        status="ready",
    )
    session.add(manifest)
    session.flush()
    for shard in request.shards:
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=shard.shard_index,
                shard_path=shard.shard_path,
                status="pending",
                file_count=shard.file_count,
            )
        )
    session.commit()
    session.refresh(manifest)
    return manifest

def _claimable_scan_unit_id_select(
    *,
    limit: int = SCAN_UNIT_CLAIM_BATCH_SIZE,
    after_id: int | None = None,
):
    statement = (
        select(ScanUnit)
        .join(Job, ScanUnit.job_id == Job.id)
        .where(ScanUnit.status.in_(RECLAIMABLE_SCAN_UNIT_STATUSES))
        .where(Job.assigned_server_id == POOL_SERVER_ID)
        .where(Job.status.in_({"queued", "running"}))
    )
    if after_id is not None:
        statement = statement.where(ScanUnit.id > after_id)
    return (
        statement.order_by(ScanUnit.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
        .with_only_columns(ScanUnit.id)
    )

def claim_next_scan_unit(session: Session, server_id: str) -> ScanUnit | None:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return None
    now = utcnow()
    reconcile_expired_scan_unit_leases(session, now=now)
    after_id: int | None = None
    while True:
        candidate_ids = session.execute(
            _claimable_scan_unit_id_select(
                limit=SCAN_UNIT_CLAIM_BATCH_SIZE,
                after_id=after_id,
            )
        ).scalars().all()
        if not candidate_ids:
            return None
        after_id = max(candidate_ids)
        for unit_id in candidate_ids:
            unit = session.get(ScanUnit, unit_id)
            if unit is None:
                continue
            job = unit.job
            if not server_is_allowed_for_job(job, server_id):
                continue
            if not server_can_access_input_dir(session, server_id, unit.path):
                continue
            result = session.execute(
                update(ScanUnit)
                .where(ScanUnit.id == unit.id)
                .where(ScanUnit.status.in_(RECLAIMABLE_SCAN_UNIT_STATUSES))
                .values(
                    status="running",
                    assigned_server_id=server_id,
                    attempt_count=ScanUnit.attempt_count + 1,
                    started_at=now,
                    lease_expires_at=scan_unit_lease_deadline(now),
                    failure_category=None,
                    error_message=None,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return claim_next_scan_unit(session, server_id)
            if job.status == "queued":
                job.status = "running"
                job.started_at = now
            session.commit()
            return session.get(ScanUnit, unit.id)
        session.rollback()

def _next_manifest_shard_index(
    session: Session,
    manifest: Manifest,
    shard_count: int,
) -> int:
    stored_next = max(int(manifest.next_shard_index or 1), 1)
    if shard_count <= 0:
        return stored_next
    conflicting_index = session.execute(
        select(WorkShard.shard_index)
        .where(WorkShard.manifest_id == manifest.id)
        .where(WorkShard.shard_index >= stored_next)
        .where(WorkShard.shard_index < stored_next + shard_count)
        .limit(1)
    ).scalar_one_or_none()
    if conflicting_index is None:
        return stored_next

    existing_max = session.execute(
        select(func.max(WorkShard.shard_index)).where(WorkShard.manifest_id == manifest.id)
    ).scalar_one()
    return max(stored_next, int(existing_max or 0) + 1)

def _manifest_for_scan_unit_completion_select(job_id: str):
    return (
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
        .with_for_update()
    )

def _existing_scan_unit_paths(session: Session, job_id: str, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    rows = session.execute(
        select(ScanUnit.path)
        .where(ScanUnit.job_id == job_id)
        .where(ScanUnit.path.in_(paths))
    ).scalars().all()
    return {str(path) for path in rows}

def complete_scan_unit(
    session: Session,
    scan_unit_id: int,
    request: ScanUnitCompleteRequest,
) -> ScanUnit:
    unit = session.execute(
        select(ScanUnit)
        .where(ScanUnit.id == scan_unit_id)
        .with_for_update()
    ).scalar_one_or_none()
    if unit is None:
        raise ValueError(f"unknown scan unit: {scan_unit_id}")
    if request.assigned_server_id is not None and request.assigned_server_id != unit.assigned_server_id:
        raise ScanUnitAttemptConflictError(
            "scan unit completion belongs to a different server attempt"
        )
    if request.attempt_count is not None and request.attempt_count != unit.attempt_count:
        raise ScanUnitAttemptConflictError(
            "scan unit completion belongs to a stale attempt"
        )
    if unit.status == "succeeded":
        session.commit()
        session.refresh(unit)
        return unit
    if unit.status != "running":
        raise ScanUnitAttemptConflictError(
            f"scan unit is not running: {unit.status}"
        )
    job = get_job_or_raise(session, unit.job_id)
    manifest = session.execute(_manifest_for_scan_unit_completion_select(job.id)).scalar_one()
    unit.status = "succeeded"
    unit.manifest_path = request.manifest_path
    unit.meta_path = request.meta_path
    unit.file_count = request.file_count
    unit.total_bytes = request.total_bytes
    unit.finished_at = utcnow()
    unit.lease_expires_at = None
    child_paths = list(dict.fromkeys(request.child_paths))
    existing_child_paths = _existing_scan_unit_paths(session, job.id, child_paths)
    for child_path in child_paths:
        if child_path in existing_child_paths:
            continue
        session.add(ScanUnit(job_id=job.id, path=child_path, status="pending"))
    next_shard_index = _next_manifest_shard_index(session, manifest, len(request.shards))
    for offset, shard in enumerate(request.shards, start=1):
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=next_shard_index + offset - 1,
                shard_path=shard.shard_path,
                status="pending",
                file_count=shard.file_count,
            )
        )
    manifest.next_shard_index = next_shard_index + len(request.shards)
    manifest.file_count = int(manifest.file_count or 0) + request.file_count
    manifest.total_bytes = int(manifest.total_bytes or 0) + request.total_bytes
    session.flush()
    freeze_manifest_if_scan_complete(session, job, manifest)
    session.commit()
    session.refresh(unit)
    return unit

def fail_scan_unit(
    session: Session,
    scan_unit_id: int,
    request: ScanUnitFailRequest,
) -> ScanUnit:
    unit = session.execute(
        select(ScanUnit)
        .where(ScanUnit.id == scan_unit_id)
        .with_for_update()
    ).scalar_one_or_none()
    if unit is None:
        raise ValueError(f"unknown scan unit: {scan_unit_id}")
    if request.assigned_server_id is not None and request.assigned_server_id != unit.assigned_server_id:
        raise ScanUnitAttemptConflictError(
            "scan unit failure belongs to a different server attempt"
        )
    if request.attempt_count is not None and request.attempt_count != unit.attempt_count:
        raise ScanUnitAttemptConflictError(
            "scan unit failure belongs to a stale attempt"
        )
    if unit.status == "failed":
        session.commit()
        session.refresh(unit)
        return unit
    if unit.status != "running":
        raise ScanUnitAttemptConflictError(
            f"scan unit is not running: {unit.status}"
        )
    job = get_job_or_raise(session, unit.job_id)
    now = utcnow()
    unit.status = "failed"
    unit.failure_category = _scan_unit_failure_category(request)
    unit.error_message = request.error_message
    unit.finished_at = now
    unit.lease_expires_at = None
    session.flush()

    active_units = session.execute(
        select(func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status.in_({"pending", "running", "stale"}))
    ).scalar_one()
    if int(active_units or 0) == 0:
        manifest = session.execute(
            select(Manifest)
            .where(Manifest.job_id == job.id)
            .order_by(Manifest.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if manifest is not None:
            manifest.status = "failed"
    session.commit()
    session.refresh(unit)
    return unit

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

class InvalidManifestRowError(ValueError):
    pass

class DuplicateManifestRelativePathError(ValueError):
    pass

class InvalidManifestRelativePathError(ValueError):
    pass

def _validate_manifest_relative_path_shape(relative_path_value: str, line_number: int) -> str:
    if "\\" in relative_path_value:
        raise InvalidManifestRelativePathError(
            f"relative_path must use POSIX '/' separators at line {line_number}"
        )
    relative_path = Path(relative_path_value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise InvalidManifestRelativePathError(
            f"relative_path must be relative and may not contain '..' at line {line_number}"
        )
    if not relative_path.name or relative_path.suffix.lower() != ".pdf":
        raise InvalidManifestRelativePathError(
            f"relative_path must point to a PDF file at line {line_number}"
        )
    return relative_path.as_posix()

def _count_jsonl_rows_with_relative_paths(path: Path) -> tuple[int, set[str], int]:
    count = 0
    total_bytes = 0
    seen_relative_paths: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if stripped:
                try:
                    item = ManifestItem.from_json_line(stripped)
                except json.JSONDecodeError:
                    raise
                except (KeyError, TypeError, ValueError) as exc:
                    raise InvalidManifestRowError(
                        f"invalid manifest row at line {line_number}"
                    ) from exc
                relative_key = _validate_manifest_relative_path_shape(
                    item.relative_path,
                    line_number,
                )
                if relative_key in seen_relative_paths:
                    raise DuplicateManifestRelativePathError(
                        f"duplicate relative_path at line {line_number}: {relative_key}"
                    )
                seen_relative_paths.add(relative_key)
                total_bytes += item.size_bytes
                count += 1
    return count, seen_relative_paths, total_bytes

def _count_jsonl_rows(path: Path) -> int:
    count, _, _ = _count_jsonl_rows_with_relative_paths(path)
    return count

def _validate_json_file(path: Path) -> str | None:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return "file_unreadable"
    except json.JSONDecodeError:
        return "malformed_json"
    return None

def _append_manifest_integrity_issue_sample(samples: list[Any], issue: Any) -> None:
    if MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT == 0:
        return
    if len(samples) < MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT:
        samples.append(issue)

def _scan_unit_status_counts(session: Session, job_id: str) -> dict[str, int]:
    rows = session.execute(
        select(ScanUnit.status, func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job_id)
        .group_by(ScanUnit.status)
    ).all()
    return {status: int(count) for status, count in rows}

def _shard_status_counts(session: Session, job_id: str) -> dict[str, int]:
    rows = session.execute(
        select(WorkShard.status, func.count(WorkShard.id))
        .where(WorkShard.job_id == job_id)
        .group_by(WorkShard.status)
    ).all()
    return {status: int(count) for status, count in rows}

def _manifest_integrity_issue_samples(
    report: ManifestIntegrityResponse,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for issue in report.bad_scan_units:
        samples.append(
            {
                "kind": "scan_unit",
                "scan_unit_id": issue.scan_unit_id,
                "path": issue.path,
                "manifest_path": issue.manifest_path,
                "expected_file_count": issue.expected_file_count,
                "actual_file_count": issue.actual_file_count,
                "reason": issue.reason,
            }
        )
        if len(samples) >= limit:
            return samples
    for issue in report.bad_shards:
        samples.append(
            {
                "kind": "shard",
                "shard_id": issue.shard_id,
                "shard_index": issue.shard_index,
                "shard_path": issue.shard_path,
                "expected_file_count": issue.expected_file_count,
                "actual_file_count": issue.actual_file_count,
                "reason": issue.reason,
            }
        )
        if len(samples) >= limit:
            return samples
    return samples

def _manifest_integrity_freeze_summary(report: ManifestIntegrityResponse) -> dict[str, Any]:
    issue_count = report.bad_scan_unit_count + report.bad_shard_count
    if not report.ok:
        if report.scan_unit_count > 0:
            if (
                not report.scan_unit_manifest_count_matches
                or not report.scan_unit_manifest_total_bytes_matches
            ):
                issue_count += 1
        else:
            if (
                not report.manifest_file_exists
                or not report.manifest_file_count_matches
                or not report.manifest_total_bytes_matches
                or report.manifest_error is not None
            ):
                issue_count += 1
            if (
                report.meta_file_exists is False
                or report.meta_error is not None
                or (report.meta_path is not None and not report.meta_file_count_matches)
                or (
                    report.meta_path is not None
                    and report.meta_actual_total_bytes is not None
                    and not report.meta_total_bytes_matches
                )
            ):
                issue_count += 1
        if not report.shard_file_count_matches_manifest:
            issue_count += 1

    return {
        "integrity_ok": report.ok,
        "integrity_status": report.status,
        "integrity_manifest_file_exists": report.manifest_file_exists,
        "integrity_manifest_file_count_matches": report.manifest_file_count_matches,
        "integrity_manifest_total_bytes_matches": report.manifest_total_bytes_matches,
        "integrity_meta_file_count_matches": report.meta_file_count_matches,
        "integrity_meta_total_bytes_matches": report.meta_total_bytes_matches,
        "integrity_scan_unit_count": report.scan_unit_count,
        "integrity_scan_unit_manifest_count_matches": report.scan_unit_manifest_count_matches,
        "integrity_scan_unit_manifest_total_bytes_matches": report.scan_unit_manifest_total_bytes_matches,
        "integrity_shard_count": report.shard_count,
        "integrity_shard_file_count_matches_manifest": report.shard_file_count_matches_manifest,
        "integrity_bad_scan_unit_count": report.bad_scan_unit_count,
        "integrity_bad_shard_count": report.bad_shard_count,
        "integrity_issue_count": issue_count,
        "integrity_issue_samples": _manifest_integrity_issue_samples(report),
    }

def _build_manifest_freeze_report(session: Session, job: Job, manifest: Manifest) -> dict[str, Any]:
    scan_unit_counts = _scan_unit_status_counts(session, job.id)
    shard_counts = _shard_status_counts(session, job.id)
    scan_progress = _latest_manifest_scan_progress(session, job.id)
    progress_scan_error_count = int(scan_progress.get("skipped_error_count") or 0)
    progress_scan_error_samples = _recent_manifest_scan_error_samples(session, job.id, limit=10)
    manifest_scan_meta = _manifest_scan_metadata(manifest)
    try:
        manifest_scan_error_count = int(manifest_scan_meta.get("skipped_error_count") or 0)
    except (TypeError, ValueError):
        manifest_scan_error_count = 0
    manifest_scan_error_samples = _manifest_scan_error_samples(manifest_scan_meta, limit=10)
    scan_unit_problem_samples = _scan_unit_problem_samples(session, job.id, limit=10)
    total_scan_units = sum(scan_unit_counts.values())
    total_shards = sum(shard_counts.values())
    shard_file_count = int(
        session.execute(
            select(func.coalesce(func.sum(WorkShard.file_count), 0)).where(
                WorkShard.job_id == job.id
            )
        ).scalar_one()
        or 0
    )
    manifest_file_count = int(manifest.file_count or 0)
    scan_error_samples = (
        progress_scan_error_samples
        or manifest_scan_error_samples
        or scan_unit_problem_samples
    )
    scan_error_count = max(
        progress_scan_error_count,
        manifest_scan_error_count,
        scan_unit_counts.get("failed", 0),
        scan_unit_counts.get("stale", 0),
        len(scan_error_samples),
    )
    integrity_summary = _manifest_integrity_freeze_summary(
        get_manifest_integrity_report(session, job.id)
    )
    return {
        "frozen": manifest.frozen_at is not None,
        "job_id": job.id,
        "manifest_id": manifest.id,
        "input_mode": manifest.input_mode,
        "input_root": manifest.input_root,
        "manifest_path": manifest.manifest_path,
        "meta_path": manifest.meta_path,
        "file_count": manifest_file_count,
        "total_bytes": int(manifest.total_bytes or 0),
        "shard_count": total_shards,
        "shard_file_count": shard_file_count,
        "shard_file_count_matches_manifest": shard_file_count == manifest_file_count,
        "scan_unit_count": total_scan_units,
        "scan_units": {
            "pending": scan_unit_counts.get("pending", 0),
            "running": scan_unit_counts.get("running", 0),
            "stale": scan_unit_counts.get("stale", 0),
            "succeeded": scan_unit_counts.get("succeeded", 0),
            "failed": scan_unit_counts.get("failed", 0),
        },
        "shards": {
            "pending": shard_counts.get("pending", 0),
            "running": shard_counts.get("running", 0),
            "retrying": shard_counts.get("retrying", 0),
            "stale": shard_counts.get("stale", 0),
            "succeeded": shard_counts.get("succeeded", 0),
            "failed": shard_counts.get("failed", 0),
            "stopped": shard_counts.get("stopped", 0),
        },
        "scan_error_count": scan_error_count,
        "scan_error_samples": scan_error_samples,
        "created_at": utcnow().isoformat(),
        **integrity_summary,
    }

def freeze_manifest_if_scan_complete(session: Session, job: Job, manifest: Manifest) -> None:
    active_units = session.execute(
        select(func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status.in_({"pending", "running", "stale"}))
    ).scalar_one()
    failed_units = session.execute(
        select(func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status == "failed")
    ).scalar_one()
    if int(active_units or 0) != 0 or int(failed_units or 0) != 0:
        return
    manifest.status = "ready"
    if manifest.frozen_at is None:
        manifest.frozen_at = utcnow()
        report = _build_manifest_freeze_report(session, job, manifest)
        report["frozen"] = True
        report["frozen_at"] = manifest.frozen_at.isoformat()
        manifest.freeze_report_json = json_dumps(report)

def get_manifest_freeze_report(session: Session, job_id: str) -> ManifestFreezeReportResponse:
    job = get_job_or_raise(session, job_id)
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if manifest is None:
        return ManifestFreezeReportResponse(
            job_id=job_id,
            manifest_id=None,
            status="missing_manifest",
            frozen_at=None,
            report={"frozen": False},
        )
    if manifest.frozen_at is not None:
        return ManifestFreezeReportResponse(
            job_id=job_id,
            manifest_id=manifest.id,
            status=manifest.status,
            frozen_at=manifest.frozen_at,
            report=json_loads_object(manifest.freeze_report_json),
        )
    return ManifestFreezeReportResponse(
        job_id=job_id,
        manifest_id=manifest.id,
        status=manifest.status,
        frozen_at=None,
        report=_build_manifest_freeze_report(session, job, manifest) | {"frozen": False},
    )

def path_is_under_worker_shared_root(session: Session, path: str | None) -> bool:
    if not path:
        return False
    servers = session.execute(
        select(Server)
        .where(Server.archived_at.is_(None))
        .where(Server.id != POOL_SERVER_ID)
    ).scalars()
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        for item in capabilities.get("shared_paths") or []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            if item.get("exists") is False:
                continue
            if _path_is_under(str(item["path"]), path):
                return True
    return False

def _server_can_read_path(server: Server, path: str | None) -> bool:
    if not path:
        return False
    access = evaluate_server_path_access(server, path)
    return bool(access.get("can_access"))

def _manifest_worker_report_payload(report: ManifestIntegrityResponse) -> dict[str, Any]:
    if hasattr(report, "model_dump"):
        return report.model_dump(mode="json")
    return report.dict()

def _load_worker_integrity_report(manifest: Manifest) -> ManifestIntegrityResponse | None:
    if not manifest.worker_integrity_report_json:
        return None
    try:
        payload = json_loads_object(manifest.worker_integrity_report_json)
    except json.JSONDecodeError:
        return None
    if not payload:
        return None
    payload = dict(payload)
    payload["source"] = "worker"
    payload["checked_by_server_id"] = manifest.worker_integrity_server_id
    payload["checked_at"] = manifest.worker_integrity_finished_at
    payload["worker_integrity_status"] = manifest.worker_integrity_status
    try:
        return ManifestIntegrityResponse(**payload)
    except ValueError:
        return None

def request_worker_manifest_integrity_check(
    session: Session,
    job_id: str,
) -> ManifestIntegrityWorkerRequestResponse:
    get_job_or_raise(session, job_id)
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if manifest is None:
        return ManifestIntegrityWorkerRequestResponse(
            job_id=job_id,
            manifest_id=None,
            worker_integrity_status="missing_manifest",
            requested_at=None,
        )
    now = utcnow()
    manifest.worker_integrity_status = "pending"
    manifest.worker_integrity_requested_at = now
    manifest.worker_integrity_started_at = None
    manifest.worker_integrity_finished_at = None
    manifest.worker_integrity_server_id = None
    manifest.worker_integrity_report_json = "{}"
    session.commit()
    return ManifestIntegrityWorkerRequestResponse(
        job_id=job_id,
        manifest_id=manifest.id,
        worker_integrity_status="pending",
        requested_at=now,
    )

def claim_worker_manifest_integrity_check(
    session: Session,
    server_id: str,
) -> ManifestIntegrityWorkerTask | None:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return None
    manifests = session.execute(
        select(Manifest)
        .where(Manifest.worker_integrity_status == "pending")
        .order_by(Manifest.worker_integrity_requested_at.asc(), Manifest.id.asc())
    ).scalars().all()
    for manifest in manifests:
        if not _server_can_read_path(server, manifest.manifest_path):
            continue
        shards = session.execute(
            select(WorkShard)
            .where(WorkShard.manifest_id == manifest.id)
            .order_by(WorkShard.shard_index.asc())
        ).scalars().all()
        now = utcnow()
        manifest.worker_integrity_status = "running"
        manifest.worker_integrity_started_at = now
        manifest.worker_integrity_server_id = server_id
        session.commit()
        return ManifestIntegrityWorkerTask(
            job_id=manifest.job_id,
            manifest_id=manifest.id,
            manifest_path=manifest.manifest_path,
            meta_path=manifest.meta_path,
            manifest_expected_file_count=int(manifest.file_count or 0),
            manifest_expected_total_bytes=int(manifest.total_bytes or 0),
            shards=[
                ManifestIntegrityWorkerShardTask(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=int(shard.file_count or 0),
                )
                for shard in shards
            ],
        )
    return None

def complete_worker_manifest_integrity_check(
    session: Session,
    manifest_id: int,
    server_id: str,
    request: ManifestIntegrityWorkerCompleteRequest,
) -> ManifestIntegrityWorkerRequestResponse:
    manifest = session.get(Manifest, manifest_id)
    if manifest is None:
        raise ValueError(f"Unknown manifest {manifest_id}")
    if manifest.worker_integrity_server_id not in (None, server_id):
        raise ValueError(
            f"Manifest integrity check {manifest_id} is assigned to {manifest.worker_integrity_server_id}"
        )
    report = request.report
    if report.manifest_id not in (None, manifest.id) or report.job_id != manifest.job_id:
        raise ValueError("Manifest integrity report does not match the claimed manifest")
    now = utcnow()
    manifest.worker_integrity_status = "ok" if report.ok else "failed"
    manifest.worker_integrity_finished_at = now
    manifest.worker_integrity_server_id = server_id
    manifest.worker_integrity_report_json = json.dumps(
        _manifest_worker_report_payload(report),
        ensure_ascii=False,
        default=str,
    )
    session.commit()
    return ManifestIntegrityWorkerRequestResponse(
        job_id=manifest.job_id,
        manifest_id=manifest.id,
        worker_integrity_status=manifest.worker_integrity_status or "unknown",
        requested_at=manifest.worker_integrity_requested_at,
    )

def get_manifest_integrity_report(session: Session, job_id: str) -> ManifestIntegrityResponse:
    get_job_or_raise(session, job_id)
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if manifest is None:
        return ManifestIntegrityResponse(
            job_id=job_id,
            manifest_id=None,
            ok=False,
            status="missing_manifest",
        )

    manifest_path = Path(manifest.manifest_path)
    manifest_file_exists = manifest_path.exists()
    control_cannot_access_manifest = (
        not manifest_file_exists
        and path_is_under_worker_shared_root(session, manifest.manifest_path)
    )
    manifest_actual_file_count: int | None = None
    manifest_file_count_matches = False
    manifest_expected_total_bytes = int(manifest.total_bytes or 0)
    manifest_actual_total_bytes: int | None = None
    manifest_total_bytes_matches = False
    manifest_error: str | None = None
    manifest_relative_paths: set[str] | None = None
    if manifest_file_exists:
        try:
            (
                manifest_actual_file_count,
                manifest_relative_paths,
                manifest_actual_total_bytes,
            ) = _count_jsonl_rows_with_relative_paths(
                manifest_path
            )
            manifest_file_count_matches = manifest_actual_file_count == manifest.file_count
            manifest_total_bytes_matches = (
                manifest_actual_total_bytes == manifest_expected_total_bytes
            )
            if manifest_file_count_matches and not manifest_total_bytes_matches:
                manifest_error = "total_bytes_mismatch"
        except OSError:
            manifest_actual_file_count = None
            manifest_error = "file_unreadable"
        except json.JSONDecodeError:
            manifest_actual_file_count = None
            manifest_error = "malformed_jsonl"
        except InvalidManifestRowError:
            manifest_actual_file_count = None
            manifest_error = "invalid_manifest_row"
        except InvalidManifestRelativePathError:
            manifest_actual_file_count = None
            manifest_error = "invalid_relative_path"
        except DuplicateManifestRelativePathError:
            manifest_actual_file_count = None
            manifest_error = "duplicate_relative_path"

    meta_file_exists: bool | None = None
    meta_error: str | None = None
    meta_expected_file_count = int(manifest.file_count or 0)
    meta_actual_file_count: int | None = None
    meta_file_count_matches = False
    meta_expected_total_bytes = int(manifest.total_bytes or 0)
    meta_actual_total_bytes: int | None = None
    meta_total_bytes_matches = False
    if manifest.meta_path:
        meta_path = Path(manifest.meta_path)
        meta_file_exists = meta_path.exists()
        if meta_file_exists:
            try:
                meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
            except OSError:
                meta_error = "file_unreadable"
            except json.JSONDecodeError:
                meta_error = "malformed_json"
            else:
                if isinstance(meta_payload, dict) and meta_payload.get("file_count") is not None:
                    try:
                        meta_actual_file_count = int(meta_payload["file_count"])
                    except (TypeError, ValueError):
                        meta_error = "file_count_invalid"
                    else:
                        meta_file_count_matches = meta_actual_file_count == meta_expected_file_count
                    if meta_error is None and meta_payload.get("total_bytes") is not None:
                        try:
                            meta_actual_total_bytes = int(meta_payload["total_bytes"])
                        except (TypeError, ValueError):
                            meta_error = "total_bytes_invalid"
                        else:
                            meta_total_bytes_matches = (
                                meta_actual_total_bytes == meta_expected_total_bytes
                            )
                            if not meta_total_bytes_matches:
                                meta_error = "total_bytes_mismatch"
                elif isinstance(meta_payload, dict):
                    meta_error = "file_count_missing"
                else:
                    meta_error = "malformed_json"

    if control_cannot_access_manifest and int(
        session.execute(
            select(func.count(ScanUnit.id))
            .where(ScanUnit.job_id == job_id)
            .where(ScanUnit.status == "succeeded")
        ).scalar_one()
        or 0
    ) == 0:
        worker_report = _load_worker_integrity_report(manifest)
        if worker_report is not None:
            return worker_report
        shard_count = int(
            session.execute(
                select(func.count(WorkShard.id)).where(WorkShard.job_id == job_id)
            ).scalar_one()
            or 0
        )
        shard_expected_file_count = int(
            session.execute(
                select(func.coalesce(func.sum(WorkShard.file_count), 0)).where(
                    WorkShard.job_id == job_id
                )
            ).scalar_one()
            or 0
        )
        return ManifestIntegrityResponse(
            job_id=job_id,
            manifest_id=manifest.id,
            ok=False,
            status="not_accessible_from_control",
            manifest_path=manifest.manifest_path,
            manifest_file_exists=False,
            manifest_expected_file_count=manifest.file_count,
            manifest_file_count_matches=False,
            manifest_expected_total_bytes=manifest.total_bytes,
            manifest_total_bytes_matches=False,
            worker_integrity_status=manifest.worker_integrity_status,
            meta_path=manifest.meta_path,
            meta_file_exists=False if manifest.meta_path else None,
            meta_expected_file_count=int(manifest.file_count or 0),
            meta_file_count_matches=False,
            meta_expected_total_bytes=int(manifest.total_bytes or 0),
            meta_total_bytes_matches=False,
            shard_count=shard_count,
            shard_expected_file_count=shard_expected_file_count,
            shard_reference_file_count=int(manifest.file_count or 0),
            shard_file_count_matches_manifest=shard_expected_file_count == int(manifest.file_count or 0),
        )

    bad_scan_unit_count = 0
    bad_scan_units: list[ManifestIntegrityScanUnitIssue] = []
    scan_units = session.execute(
        select(ScanUnit)
        .where(ScanUnit.job_id == job_id)
        .where(ScanUnit.status == "succeeded")
        .order_by(ScanUnit.id.asc())
    ).scalars().all()
    scan_unit_expected_file_count = sum(int(unit.file_count or 0) for unit in scan_units)
    scan_unit_expected_total_bytes = sum(int(unit.total_bytes or 0) for unit in scan_units)
    scan_unit_actual_file_count = 0
    scan_unit_actual_total_bytes = 0
    scan_unit_actual_count_known = True
    scan_unit_relative_paths: set[str] = set()
    for unit in scan_units:
        if not unit.manifest_path:
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=None,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="manifest_path_missing",
                )
            )
            scan_unit_actual_count_known = False
            continue
        unit_manifest_path = Path(unit.manifest_path)
        if not unit_manifest_path.exists():
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="file_missing",
                )
            )
            scan_unit_actual_count_known = False
            continue
        try:
            (
                unit_actual_file_count,
                unit_relative_paths,
                unit_actual_total_bytes,
            ) = _count_jsonl_rows_with_relative_paths(
                unit_manifest_path
            )
        except OSError:
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="file_unreadable",
                )
            )
            scan_unit_actual_count_known = False
            continue
        except json.JSONDecodeError:
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="malformed_jsonl",
                )
            )
            scan_unit_actual_count_known = False
            continue
        except InvalidManifestRowError:
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="invalid_manifest_row",
                )
            )
            scan_unit_actual_count_known = False
            continue
        except InvalidManifestRelativePathError:
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="invalid_relative_path",
                )
            )
            scan_unit_actual_count_known = False
            continue
        except DuplicateManifestRelativePathError:
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="duplicate_relative_path",
                )
            )
            scan_unit_actual_count_known = False
            continue
        if scan_unit_relative_paths.intersection(unit_relative_paths):
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="duplicate_relative_path",
                )
            )
            scan_unit_actual_count_known = False
            continue
        scan_unit_relative_paths.update(unit_relative_paths)
        scan_unit_actual_file_count += unit_actual_file_count
        if unit_actual_file_count != unit.file_count:
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=unit_actual_file_count,
                    reason="file_count_mismatch",
                )
            )
        if unit_actual_total_bytes != int(unit.total_bytes or 0):
            bad_scan_unit_count += 1
            _append_manifest_integrity_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=unit_actual_file_count,
                    reason="total_bytes_mismatch",
                )
            )
            scan_unit_actual_count_known = False
            continue
        scan_unit_actual_total_bytes += unit_actual_total_bytes
        if unit.meta_path:
            unit_meta_path = Path(unit.meta_path)
            if not unit_meta_path.exists():
                bad_scan_unit_count += 1
                _append_manifest_integrity_issue_sample(
                    bad_scan_units,
                    ManifestIntegrityScanUnitIssue(
                        scan_unit_id=unit.id,
                        path=unit.path,
                        manifest_path=unit.meta_path,
                        expected_file_count=unit.file_count,
                        actual_file_count=unit_actual_file_count,
                        reason="meta_file_missing",
                    )
                )
            else:
                try:
                    unit_meta_payload = json.loads(unit_meta_path.read_text(encoding="utf-8"))
                except OSError:
                    unit_meta_error = "file_unreadable"
                    unit_meta_payload = None
                except json.JSONDecodeError:
                    unit_meta_error = "malformed_json"
                    unit_meta_payload = None
                else:
                    unit_meta_error = None if isinstance(unit_meta_payload, dict) else "malformed_json"
                if unit_meta_error:
                    unit_meta_reason = (
                        "meta_file_malformed"
                        if unit_meta_error == "malformed_json"
                        else "meta_file_unreadable"
                    )
                    bad_scan_unit_count += 1
                    _append_manifest_integrity_issue_sample(
                        bad_scan_units,
                        ManifestIntegrityScanUnitIssue(
                            scan_unit_id=unit.id,
                            path=unit.path,
                            manifest_path=unit.meta_path,
                            expected_file_count=unit.file_count,
                            actual_file_count=unit_actual_file_count,
                            reason=unit_meta_reason,
                        )
                    )
                elif isinstance(unit_meta_payload, dict) and unit_meta_payload.get("file_count") is not None:
                    try:
                        unit_meta_file_count = int(unit_meta_payload["file_count"])
                    except (TypeError, ValueError):
                        bad_scan_unit_count += 1
                        _append_manifest_integrity_issue_sample(
                            bad_scan_units,
                            ManifestIntegrityScanUnitIssue(
                                scan_unit_id=unit.id,
                                path=unit.path,
                                manifest_path=unit.meta_path,
                                expected_file_count=unit.file_count,
                                actual_file_count=unit_actual_file_count,
                                reason="meta_file_count_invalid",
                            )
                        )
                    else:
                        if unit_meta_file_count != int(unit.file_count or 0):
                            bad_scan_unit_count += 1
                            _append_manifest_integrity_issue_sample(
                                bad_scan_units,
                                ManifestIntegrityScanUnitIssue(
                                    scan_unit_id=unit.id,
                                    path=unit.path,
                                    manifest_path=unit.meta_path,
                                    expected_file_count=unit.file_count,
                                    actual_file_count=unit_meta_file_count,
                                    reason="meta_file_count_mismatch",
                                )
                            )
                if isinstance(unit_meta_payload, dict) and unit_meta_payload.get("total_bytes") is not None:
                    try:
                        unit_meta_total_bytes = int(unit_meta_payload["total_bytes"])
                    except (TypeError, ValueError):
                        bad_scan_unit_count += 1
                        _append_manifest_integrity_issue_sample(
                            bad_scan_units,
                            ManifestIntegrityScanUnitIssue(
                                scan_unit_id=unit.id,
                                path=unit.path,
                                manifest_path=unit.meta_path,
                                expected_file_count=unit.file_count,
                                actual_file_count=unit_actual_file_count,
                                reason="meta_total_bytes_invalid",
                            )
                        )
                    else:
                        if unit_meta_total_bytes != int(unit.total_bytes or 0):
                            bad_scan_unit_count += 1
                            _append_manifest_integrity_issue_sample(
                                bad_scan_units,
                                ManifestIntegrityScanUnitIssue(
                                    scan_unit_id=unit.id,
                                    path=unit.path,
                                    manifest_path=unit.meta_path,
                                    expected_file_count=unit.file_count,
                                    actual_file_count=unit_actual_file_count,
                                    reason="meta_total_bytes_mismatch",
                                )
                            )

    has_distributed_scan_units = bool(scan_units)
    scan_unit_manifest_actual_total = (
        scan_unit_actual_file_count if scan_unit_actual_count_known else None
    )
    scan_unit_manifest_actual_total_bytes = (
        scan_unit_actual_total_bytes if scan_unit_actual_count_known else None
    )
    scan_unit_manifest_count_matches = (
        has_distributed_scan_units
        and scan_unit_actual_count_known
        and scan_unit_actual_file_count == scan_unit_expected_file_count
        and scan_unit_expected_file_count == int(manifest.file_count or 0)
        and bad_scan_unit_count == 0
    )
    scan_unit_manifest_total_bytes_matches = (
        has_distributed_scan_units
        and scan_unit_actual_count_known
        and scan_unit_actual_total_bytes == scan_unit_expected_total_bytes
        and scan_unit_expected_total_bytes == int(manifest.total_bytes or 0)
        and bad_scan_unit_count == 0
    )

    bad_shard_count = 0
    bad_shards: list[ManifestIntegrityShardIssue] = []
    shards = session.execute(
        select(WorkShard)
        .where(WorkShard.job_id == job_id)
        .order_by(WorkShard.shard_index.asc())
    ).scalars().all()
    shard_expected_file_count = sum(int(shard.file_count or 0) for shard in shards)
    shard_relative_paths: set[str] = set()
    for shard in shards:
        shard_path = Path(shard.shard_path)
        if not shard_path.exists():
            bad_shard_count += 1
            _append_manifest_integrity_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="file_missing",
                )
            )
            continue
        try:
            (
                actual_file_count,
                shard_file_relative_paths,
                _shard_total_bytes,
            ) = _count_jsonl_rows_with_relative_paths(
                shard_path
            )
        except OSError:
            bad_shard_count += 1
            _append_manifest_integrity_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="file_unreadable",
                )
            )
            continue
        except json.JSONDecodeError:
            bad_shard_count += 1
            _append_manifest_integrity_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="malformed_jsonl",
                )
            )
            continue
        except InvalidManifestRowError:
            bad_shard_count += 1
            _append_manifest_integrity_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="invalid_manifest_row",
                )
            )
            continue
        except InvalidManifestRelativePathError:
            bad_shard_count += 1
            _append_manifest_integrity_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="invalid_relative_path",
                )
            )
            continue
        except DuplicateManifestRelativePathError:
            bad_shard_count += 1
            _append_manifest_integrity_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="duplicate_relative_path",
                )
            )
            continue
        if shard_relative_paths.intersection(shard_file_relative_paths):
            bad_shard_count += 1
            _append_manifest_integrity_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="duplicate_relative_path",
                )
            )
            continue
        shard_reference_relative_paths = (
            scan_unit_relative_paths if has_distributed_scan_units else manifest_relative_paths
        )
        if (
            shard_reference_relative_paths is not None
            and shard_file_relative_paths.difference(shard_reference_relative_paths)
        ):
            bad_shard_count += 1
            _append_manifest_integrity_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="relative_path_not_in_manifest",
                )
            )
            continue
        shard_relative_paths.update(shard_file_relative_paths)
        if actual_file_count != shard.file_count:
            bad_shard_count += 1
            _append_manifest_integrity_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=actual_file_count,
                    reason="file_count_mismatch",
                )
            )

    if has_distributed_scan_units:
        manifest_ok = scan_unit_manifest_count_matches
        manifest_total_ok = scan_unit_manifest_total_bytes_matches
        meta_ok = True
        shard_reference_file_count = scan_unit_expected_file_count
    else:
        manifest_ok = manifest_file_exists and manifest_file_count_matches
        manifest_total_ok = (
            manifest_file_exists
            and manifest_error is None
            and manifest_total_bytes_matches
        )
        meta_ok = (
            meta_file_exists is not False
            and meta_error is None
            and (manifest.meta_path is None or meta_file_count_matches)
            and (
                manifest.meta_path is None
                or meta_actual_total_bytes is None
                or meta_total_bytes_matches
            )
        )
        shard_reference_file_count = int(manifest.file_count or 0)
    shard_file_count_matches_manifest = shard_expected_file_count == shard_reference_file_count
    ok = (
        manifest_ok
        and manifest_total_ok
        and meta_ok
        and shard_file_count_matches_manifest
        and bad_scan_unit_count == 0
        and bad_shard_count == 0
    )
    return ManifestIntegrityResponse(
        job_id=job_id,
        manifest_id=manifest.id,
        ok=ok,
        status="ok" if ok else "failed",
        manifest_path=manifest.manifest_path,
        manifest_file_exists=manifest_file_exists,
        manifest_expected_file_count=manifest.file_count,
        manifest_actual_file_count=manifest_actual_file_count,
        manifest_file_count_matches=manifest_file_count_matches,
        manifest_expected_total_bytes=manifest_expected_total_bytes,
        manifest_actual_total_bytes=manifest_actual_total_bytes,
        manifest_total_bytes_matches=manifest_total_bytes_matches,
        manifest_error=manifest_error,
        meta_path=manifest.meta_path,
        meta_file_exists=meta_file_exists,
        meta_error=meta_error,
        meta_expected_file_count=meta_expected_file_count,
        meta_actual_file_count=meta_actual_file_count,
        meta_file_count_matches=meta_file_count_matches,
        meta_expected_total_bytes=meta_expected_total_bytes,
        meta_actual_total_bytes=meta_actual_total_bytes,
        meta_total_bytes_matches=meta_total_bytes_matches,
        scan_unit_count=len(scan_units),
        scan_unit_manifest_expected_file_count=scan_unit_expected_file_count,
        scan_unit_manifest_actual_file_count=scan_unit_manifest_actual_total,
        scan_unit_manifest_count_matches=scan_unit_manifest_count_matches,
        scan_unit_manifest_expected_total_bytes=scan_unit_expected_total_bytes,
        scan_unit_manifest_actual_total_bytes=scan_unit_manifest_actual_total_bytes,
        scan_unit_manifest_total_bytes_matches=scan_unit_manifest_total_bytes_matches,
        bad_scan_unit_count=bad_scan_unit_count,
        bad_scan_units=bad_scan_units,
        shard_count=len(shards),
        shard_expected_file_count=shard_expected_file_count,
        shard_reference_file_count=shard_reference_file_count,
        shard_file_count_matches_manifest=shard_file_count_matches_manifest,
        bad_shard_count=bad_shard_count,
        bad_shards=bad_shards,
    )

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

def stop_reclaimable_work_for_job(session: Session, job: Job) -> None:
    current_time = utcnow()
    session.execute(
        update(WorkShard)
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
        .values(
            status="stopped",
            failure_category="operator_stopped",
            lease_expires_at=None,
            finished_at=current_time,
        )
    )
    session.execute(
        update(ScanUnit)
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status.in_(RECLAIMABLE_SCAN_UNIT_STATUSES))
        .values(
            status="stopped",
            failure_category="operator_stopped",
            lease_expires_at=None,
            finished_at=current_time,
        )
    )

def finalize_stopped_job_if_idle(session: Session, job: Job) -> bool:
    if not job.stop_requested or job.status in TERMINAL_JOB_STATUSES:
        return False
    total_shards = int(
        session.execute(
            select(func.count(WorkShard.id)).where(WorkShard.job_id == job.id)
        ).scalar_one()
        or 0
    )
    total_scan_units = int(
        session.execute(
            select(func.count(ScanUnit.id)).where(ScanUnit.job_id == job.id)
        ).scalar_one()
        or 0
    )
    if total_shards == 0 and total_scan_units == 0:
        return False
    open_shards = int(
        session.execute(
            select(func.count(WorkShard.id))
            .where(WorkShard.job_id == job.id)
            .where(WorkShard.status.not_in(TERMINAL_SHARD_STATUSES))
        ).scalar_one()
        or 0
    )
    open_scan_units = int(
        session.execute(
            select(func.count(ScanUnit.id))
            .where(ScanUnit.job_id == job.id)
            .where(ScanUnit.status.in_({"pending", "running", "stale"}))
        ).scalar_one()
        or 0
    )
    if open_shards or open_scan_units:
        return False
    job.status = "stopped"
    if job.failure_category is None:
        job.failure_category = "operator_stopped"
    if job.finished_at is None:
        job.finished_at = utcnow()
    return True

def _claimable_shard_id_select(job_id: str):
    return (
        select(WorkShard.id)
        .where(WorkShard.job_id == job_id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
        .order_by(WorkShard.shard_index.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )

def _create_shard_attempt(session: Session, shard: WorkShard, server_id: str) -> None:
    session.add(
        ShardAttempt(
            job_id=shard.job_id,
            shard_id=shard.id,
            attempt_number=shard.attempt_count,
            server_id=server_id,
            status="running",
            processed_files=shard.processed_files,
            failed_files=shard.failed_files,
            skipped_files=shard.skipped_files,
            completed_pages=shard.completed_pages,
            execution_paused=shard.execution_paused,
            api_concurrency_limit=shard.api_concurrency_limit,
            execution_control_reason=shard.execution_control_reason,
            started_at=shard.started_at or utcnow(),
        )
    )

def _latest_shard_attempt(session: Session, shard: WorkShard) -> ShardAttempt | None:
    return session.execute(
        select(ShardAttempt)
        .where(ShardAttempt.shard_id == shard.id)
        .where(ShardAttempt.attempt_number == shard.attempt_count)
        .order_by(ShardAttempt.id.desc())
        .limit(1)
    ).scalar_one_or_none()

def claim_next_pending_shard(session: Session, job_id: str, server_id: str) -> WorkShard | None:
    job = get_job_or_raise(session, job_id)
    non_claimable_statuses = {"stopping", *TERMINAL_JOB_STATUSES}
    if job.stop_requested or job.status in non_claimable_statuses:
        return None
    if job.assigned_server_id == POOL_SERVER_ID and not server_can_access_input_dir(
        session,
        server_id,
        job.input_dir,
    ):
        return None
    if job.assigned_server_id == POOL_SERVER_ID and not server_is_allowed_for_job(
        job,
        server_id,
    ):
        return None

    now = utcnow()
    reconcile_expired_shard_leases(session, now=now, job_id=job_id)
    shard_id = session.execute(_claimable_shard_id_select(job_id)).scalar_one_or_none()
    if shard_id is None:
        return None

    started_at = now
    lease_expires_at = shard_lease_deadline(now)
    claimable_parent = (
        select(Job.id)
        .where(Job.id == job_id)
        .where(Job.stop_requested.is_(False))
        .where(Job.status.not_in(non_claimable_statuses))
        .exists()
    )
    claim_stmt = (
        update(WorkShard)
        .where(WorkShard.id == shard_id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
        .where(claimable_parent)
        .values(
            status="running",
            assigned_server_id=server_id,
            failure_category=None,
            error_message=None,
            attempt_count=WorkShard.attempt_count + 1,
            started_at=started_at,
            finished_at=None,
            lease_expires_at=lease_expires_at,
        )
    )
    result = session.execute(claim_stmt)
    if result.rowcount != 1:
        session.rollback()
        parent_claimable = session.execute(select(claimable_parent)).scalar_one()
        if parent_claimable:
            return claim_next_pending_shard(session, job_id, server_id)
        return None

    shard = session.get(WorkShard, shard_id)
    if shard is not None:
        session.refresh(shard)
        _create_shard_attempt(session, shard, server_id)
    session.commit()
    return session.get(WorkShard, shard_id)

def update_work_shard(session: Session, shard_id: int, request: WorkShardUpdateRequest) -> WorkShard:
    shard = session.execute(
        select(WorkShard)
        .where(WorkShard.id == shard_id)
        .with_for_update()
    ).scalar_one_or_none()
    if shard is None:
        raise ValueError(f"unknown shard: {shard_id}")
    if request.assigned_server_id is not None and request.assigned_server_id != shard.assigned_server_id:
        raise ShardAttemptConflictError(
            "shard update belongs to a different server attempt"
        )
    if request.attempt_count is not None and request.attempt_count != shard.attempt_count:
        raise ShardAttemptConflictError(
            "shard update belongs to a stale attempt"
        )
    job = get_job_or_raise(session, shard.job_id)
    fields_set = getattr(request, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(request, "__fields_set__", set())

    shard.status = request.status
    if "processed_files" in fields_set:
        shard.processed_files = request.processed_files
    if "failed_files" in fields_set:
        shard.failed_files = request.failed_files
    if "skipped_files" in fields_set:
        shard.skipped_files = request.skipped_files
    if "completed_pages" in fields_set:
        shard.completed_pages = request.completed_pages
    if request.api_inflight is not None:
        shard.api_inflight = request.api_inflight
    if request.api_inflight_peak is not None:
        shard.api_inflight_peak = request.api_inflight_peak
    if request.api_waiting is not None:
        shard.api_waiting = request.api_waiting
    if request.oldest_api_inflight is not None:
        shard.oldest_api_inflight = request.oldest_api_inflight
    if request.execution_paused is not None:
        shard.execution_paused = request.execution_paused
    if request.api_concurrency_limit is not None:
        shard.api_concurrency_limit = request.api_concurrency_limit
    if request.execution_control_reason is not None:
        shard.execution_control_reason = request.execution_control_reason
    failure_category = request.failure_category
    if failure_category is None and request.status == "failed":
        failure_category = infer_failure_category(
            {"error_message": request.error_message}
        )
    shard.failure_category = failure_category
    shard.error_message = request.error_message
    if request.status == "failed":
        shard.status = _remaining_retry_status(job, shard)
    if shard.status in TERMINAL_SHARD_STATUSES:
        shard.finished_at = utcnow()
        shard.lease_expires_at = None
    elif shard.status in {"retrying", "stale"}:
        shard.finished_at = None
        shard.lease_expires_at = None
    attempt = _latest_shard_attempt(session, shard)
    if attempt is not None:
        attempt.status = shard.status
        attempt.processed_files = shard.processed_files
        attempt.failed_files = shard.failed_files
        attempt.skipped_files = shard.skipped_files
        attempt.completed_pages = shard.completed_pages
        attempt.execution_paused = shard.execution_paused
        attempt.api_concurrency_limit = shard.api_concurrency_limit
        attempt.execution_control_reason = shard.execution_control_reason
        attempt.failure_category = failure_category
        attempt.error_message = request.error_message
        if shard.status != "running":
            attempt.finished_at = utcnow()
    if shard.status == "failed":
        open_shards = session.execute(
            select(func.count(WorkShard.id))
            .where(WorkShard.job_id == shard.job_id)
            .where(WorkShard.id != shard.id)
            .where(WorkShard.status.not_in(TERMINAL_SHARD_STATUSES))
        ).scalar_one()
        if not open_shards and job.status not in TERMINAL_JOB_STATUSES:
            job.status = "failed"
            job.failure_category = shard.failure_category
            job.error_message = shard.error_message
            job.finished_at = utcnow()
    finalize_stopped_job_if_idle(session, job)
    session.commit()
    session.refresh(shard)
    return shard

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

__all__ = [name for name in globals() if not name.startswith("__")]
