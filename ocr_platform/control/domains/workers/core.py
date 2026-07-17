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

def _resolve_model_profile_api_key(*args, **kwargs):
    from ..model_profiles.core import _resolve_model_profile_api_key as target
    return target(*args, **kwargs)

def ensure_default_model_profiles(*args, **kwargs):
    from ..model_profiles.core import ensure_default_model_profiles as target
    return target(*args, **kwargs)

def infer_default_manifest_root(*args, **kwargs):
    from ..manifests.core import infer_default_manifest_root as target
    return target(*args, **kwargs)

def server_can_access_input_dir(*args, **kwargs):
    from ..manifests.core import server_can_access_input_dir as target
    return target(*args, **kwargs)

def stop_reclaimable_work_for_job(*args, **kwargs):
    from ..manifests.core import stop_reclaimable_work_for_job as target
    return target(*args, **kwargs)


def allowed_server_ids_for_job(job: Job) -> list[str]:
    return json_loads_list(job.allowed_server_ids_json)

def server_is_allowed_for_job(job: Job, server_id: str) -> bool:
    allowed_server_ids = allowed_server_ids_for_job(job)
    return not allowed_server_ids or server_id in allowed_server_ids

def register_server(session: Session, request: ServerRegisterRequest) -> Server:
    server = session.get(Server, request.id)
    if server is None:
        server = Server(id=request.id, name=request.name, host=request.host)
        session.add(server)

    server.name = request.name
    server.host = request.host
    server.capacity_slots = request.capacity_slots
    server.capabilities_json = json_dumps(request.capabilities)
    server.status = "online"
    server.last_heartbeat_at = utcnow()
    server.archived_at = None
    session.commit()
    session.refresh(server)
    return server

def ensure_pool_server(session: Session) -> Server:
    server = session.get(Server, POOL_SERVER_ID)
    if server is None:
        server = Server(
            id=POOL_SERVER_ID,
            name="Server Pool",
            host="pool",
            status="online",
            capacity_slots=0,
            capabilities_json=json_dumps({"pool": True}),
            archived_at=None,
        )
        session.add(server)
        session.flush()
    elif server.archived_at is not None:
        server.archived_at = None
    return server

def public_assigned_server_id(job: Job) -> str | None:
    return None if job.assigned_server_id == POOL_SERVER_ID else job.assigned_server_id

def heartbeat_server(session: Session, server_id: str, request: ServerHeartbeatRequest) -> Server:
    server = session.get(Server, server_id)
    if server is None:
        server = Server(id=server_id, name=server_id, host=server_id)
        session.add(server)

    existing_capabilities = json_loads_object(server.capabilities_json)
    merged_capabilities = {**existing_capabilities, **request.capabilities}
    server.status = request.status
    server.capabilities_json = json_dumps(merged_capabilities)
    server.last_heartbeat_at = utcnow()
    server.archived_at = None
    renew_running_shard_leases(session, server_id)
    renew_running_scan_unit_leases(session, server_id)
    session.commit()
    session.refresh(server)
    return server

def shard_lease_deadline(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=SHARD_LEASE_SECONDS)

def scan_unit_lease_deadline(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=SHARD_LEASE_SECONDS)

def _expired_running_shard_filter(now: datetime):
    return (
        (WorkShard.status == "running")
        & (WorkShard.lease_expires_at.is_not(None))
        & (WorkShard.lease_expires_at <= now)
    )

def reconcile_expired_shard_leases(session: Session, *, now: datetime | None = None, job_id: str | None = None) -> None:
    current_time = now or utcnow()
    expired_shard_probe = (
        select(WorkShard.id)
        .where(_expired_running_shard_filter(current_time))
        .limit(1)
    )
    if job_id is not None:
        expired_shard_probe = expired_shard_probe.where(WorkShard.job_id == job_id)
    if session.execute(expired_shard_probe).scalar_one_or_none() is None:
        return

    stopped_parent = (
        select(Job.id)
        .where(Job.id == WorkShard.job_id)
        .where(
            (Job.stop_requested.is_(True))
            | (Job.status == "stopping")
        )
        .exists()
    )
    current_shard_attempt_number = (
        select(WorkShard.attempt_count)
        .where(WorkShard.id == ShardAttempt.shard_id)
        .scalar_subquery()
    )
    stopped_shard_ids = (
        select(WorkShard.id)
        .join(Job, Job.id == WorkShard.job_id)
        .where(_expired_running_shard_filter(current_time))
        .where((Job.stop_requested.is_(True)) | (Job.status == "stopping"))
    )
    if job_id is not None:
        stopped_shard_ids = stopped_shard_ids.where(WorkShard.job_id == job_id)
    session.execute(
        update(ShardAttempt)
        .where(ShardAttempt.shard_id.in_(stopped_shard_ids))
        .where(ShardAttempt.attempt_number == current_shard_attempt_number)
        .where(ShardAttempt.status == "running")
        .values(
            status="stopped",
            failure_category="operator_stopped",
            finished_at=current_time,
        )
    )
    stop_stmt = (
        update(WorkShard)
        .where(_expired_running_shard_filter(current_time))
        .where(stopped_parent)
        .values(
            status="stopped",
            failure_category="operator_stopped",
            lease_expires_at=None,
            finished_at=current_time,
        )
    )
    if job_id is not None:
        stop_stmt = stop_stmt.where(WorkShard.job_id == job_id)
    session.execute(stop_stmt)

    stale_shard_ids = (
        select(WorkShard.id)
        .where(_expired_running_shard_filter(current_time))
    )
    if job_id is not None:
        stale_shard_ids = stale_shard_ids.where(WorkShard.job_id == job_id)
    session.execute(
        update(ShardAttempt)
        .where(ShardAttempt.shard_id.in_(stale_shard_ids))
        .where(ShardAttempt.attempt_number == current_shard_attempt_number)
        .where(ShardAttempt.status == "running")
        .values(
            status="stale",
            failure_category="lease_expired",
            finished_at=current_time,
        )
    )
    stale_stmt = (
        update(WorkShard)
        .where(_expired_running_shard_filter(current_time))
        .values(status="stale", lease_expires_at=None)
    )
    if job_id is not None:
        stale_stmt = stale_stmt.where(WorkShard.job_id == job_id)
    session.execute(stale_stmt)

def reconcile_expired_scan_unit_leases(session: Session, *, now: datetime | None = None, job_id: str | None = None) -> None:
    current_time = now or utcnow()
    stmt = (
        update(ScanUnit)
        .where(ScanUnit.status == "running")
        .where(ScanUnit.lease_expires_at.is_not(None))
        .where(ScanUnit.lease_expires_at <= current_time)
        .values(
            status="stale",
            lease_expires_at=None,
            failure_category="lease_expired",
            error_message="scan unit lease expired",
        )
    )
    if job_id is not None:
        stmt = stmt.where(ScanUnit.job_id == job_id)
    session.execute(stmt)

def _remaining_retry_status(job: Job, shard: WorkShard) -> str:
    return "retrying" if shard.attempt_count < job.max_shard_attempts else "failed"

def renew_running_shard_leases(session: Session, server_id: str) -> None:
    session.execute(
        update(WorkShard)
        .where(WorkShard.assigned_server_id == server_id)
        .where(WorkShard.status == "running")
        .values(lease_expires_at=shard_lease_deadline())
    )

def renew_running_scan_unit_leases(session: Session, server_id: str) -> None:
    session.execute(
        update(ScanUnit)
        .where(ScanUnit.assigned_server_id == server_id)
        .where(ScanUnit.status == "running")
        .values(lease_expires_at=scan_unit_lease_deadline())
    )

def is_server_stale(server: Server, now: datetime | None = None) -> bool:
    if server.last_heartbeat_at is None:
        return False
    return (now or utcnow()) - server.last_heartbeat_at > timedelta(seconds=SERVER_STALE_AFTER_SECONDS)

def effective_server_status(server: Server, now: datetime | None = None) -> str:
    if is_server_stale(server, now):
        return "offline"
    return server.status

def count_active_jobs_for_server(session: Session, server_id: str) -> int:
    return int(
        session.execute(
            select(func.count(Job.id))
            .where(Job.assigned_server_id == server_id)
            .where(Job.status.in_({"running", "stopping"}))
        ).scalar_one()
        or 0
    )

def count_open_jobs_for_server(session: Session, server_id: str) -> int:
    return int(
        session.execute(
            select(func.count(Job.id))
            .where(Job.assigned_server_id == server_id)
            .where(Job.status.not_in(TERMINAL_JOB_STATUSES))
        ).scalar_one()
        or 0
    )

def stop_assigned_queued_jobs_for_server(session: Session, server_id: str) -> None:
    current_time = utcnow()
    jobs = list(
        session.execute(
            select(Job)
            .where(Job.assigned_server_id == server_id)
            .where(Job.status == "queued")
        ).scalars()
    )
    for job in jobs:
        job.stop_requested = True
        job.status = "stopped"
        if job.failure_category is None:
            job.failure_category = "operator_stopped"
        if job.finished_at is None:
            job.finished_at = current_time
        stop_reclaimable_work_for_job(session, job)
    if jobs:
        session.flush()

def count_running_shards_for_server(session: Session, server_id: str) -> int:
    return int(
        session.execute(
            select(func.count(WorkShard.id))
            .where(WorkShard.assigned_server_id == server_id)
            .where(WorkShard.status == "running")
        ).scalar_one()
        or 0
    )

def _normal_posix_path(path: str) -> str:
    normalized = posixpath.normpath(path)
    return normalized if normalized.startswith("/") else f"/{normalized}"

def _path_is_under(root: str, candidate: str) -> bool:
    normalized_root = _normal_posix_path(root).rstrip("/")
    normalized_candidate = _normal_posix_path(candidate)
    if not normalized_root:
        normalized_root = "/"
    return normalized_candidate == normalized_root or normalized_candidate.startswith(
        normalized_root + "/"
    )

def evaluate_server_path_access(
    server: Server,
    input_dir: str,
    *,
    require_writable: bool = False,
) -> dict[str, Any]:
    if server.archived_at is not None:
        return {
            "server_id": server.id,
            "name": server.name,
            "host": server.host,
            "status": "archived",
            "is_stale": True,
            "can_access": False,
            "matched_path": None,
            "reason": "server_archived",
        }

    status = effective_server_status(server)
    stale = is_server_stale(server)
    if status == "offline" or stale:
        return {
            "server_id": server.id,
            "name": server.name,
            "host": server.host,
            "status": status,
            "is_stale": stale,
            "can_access": False,
            "matched_path": None,
            "reason": "server_offline",
        }

    capabilities = json_loads_object(server.capabilities_json)
    checks = capabilities.get("shared_paths") or []
    if not isinstance(checks, list) or not checks:
        return {
            "server_id": server.id,
            "name": server.name,
            "host": server.host,
            "status": status,
            "is_stale": stale,
            "can_access": False,
            "matched_path": None,
            "reason": "no_path_checks",
        }

    matched_unavailable = None
    for check in checks:
        if not isinstance(check, dict) or not check.get("path"):
            continue
        path = str(check["path"])
        if not _path_is_under(path, input_dir):
            continue
        has_required_access = (
            check.get("exists")
            and check.get("is_dir")
            and check.get("readable")
            and (not require_writable or check.get("writable"))
        )
        if has_required_access:
            return {
                "server_id": server.id,
                "name": server.name,
                "host": server.host,
                "status": status,
                "is_stale": stale,
                "can_access": True,
                "matched_path": path,
                "reason": "ok",
            }
        matched_unavailable = path

    reason = "shared_root_unavailable" if matched_unavailable else "no_matching_shared_root"
    if matched_unavailable and require_writable:
        reason = "shared_root_not_writable"
    return {
        "server_id": server.id,
        "name": server.name,
        "host": server.host,
        "status": status,
        "is_stale": stale,
        "can_access": False,
        "matched_path": matched_unavailable,
        "reason": reason,
    }

def list_server_eligibility(session: Session, input_dir: str) -> list[dict[str, Any]]:
    return [
        evaluate_server_path_access(server, input_dir)
        for server in list_servers(session)
    ]

def _preflight_issue(
    severity: str,
    code: str,
    message: str,
    **details: Any,
) -> JobPreflightIssue:
    return JobPreflightIssue(
        severity=severity,
        code=code,
        message=message,
        details=details,
    )

def _database_migration_preflight_issue(database_status: dict[str, Any]) -> JobPreflightIssue | None:
    dialect = str(database_status.get("dialect") or "")
    if dialect != "postgresql":
        return None

    known_migrations = [str(item) for item in database_status.get("known_migrations") or []]
    latest_known_migration = known_migrations[-1] if known_migrations else None
    if not database_status.get("schema_migrations_table_exists"):
        return _preflight_issue(
            "error",
            "database_migrations_missing",
            "PostgreSQL control database is missing schema_migrations; apply the SQL migration baseline before creating production jobs.",
            dialect=dialect,
            known_migrations=known_migrations,
            latest_known_migration=latest_known_migration,
        )

    applied_versions: set[str] = set()
    for item in database_status.get("applied_migrations") or []:
        if isinstance(item, dict) and item.get("version"):
            applied_versions.add(str(item["version"]))
        elif item:
            applied_versions.add(str(item))
    missing_migrations = [version for version in known_migrations if version not in applied_versions]
    if missing_migrations:
        return _preflight_issue(
            "error",
            "database_migration_not_current",
            "PostgreSQL control database has unapplied SQL migrations; apply migrations before creating production jobs.",
            dialect=dialect,
            known_migrations=known_migrations,
            missing_migrations=missing_migrations,
            latest_known_migration=latest_known_migration,
            latest_applied_migration=database_status.get("latest_applied_migration"),
        )

    checksum_mismatches = database_status.get("checksum_mismatches") or []
    missing_checksums = database_status.get("missing_checksums") or []
    unexpected_migrations = database_status.get("unexpected_migrations") or []
    if checksum_mismatches or missing_checksums or unexpected_migrations:
        return _preflight_issue(
            "error",
            "database_migration_verification_failed",
            "PostgreSQL control database migration history failed checksum verification.",
            dialect=dialect,
            checksum_mismatches=checksum_mismatches,
            missing_checksums=missing_checksums,
            unexpected_migrations=unexpected_migrations,
        )

    return None

def _control_api_auth_preflight_issue() -> JobPreflightIssue | None:
    if os.environ.get("OCR_PLATFORM_API_TOKEN"):
        return None
    return _preflight_issue(
        "warning",
        "control_api_auth_disabled",
        "Control API token authentication is not configured; set OCR_PLATFORM_API_TOKEN before exposing production endpoints.",
        require_api_token=_env_truthy("OCR_PLATFORM_REQUIRE_API_TOKEN"),
    )

def _server_versions(session: Session, server_ids: set[str]) -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {}
    if not server_ids:
        return versions
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
    ).scalars().all()
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        key = " / ".join(
            [
                str(capabilities.get("git_ref") or "unknown git"),
                str(capabilities.get("script_version") or "unknown script"),
            ]
        )
        versions.setdefault(key, []).append(server.id)
    return versions

def _job_worker_server_ids(session: Session, job: Job) -> set[str]:
    server_ids = {
        str(server_id)
        for server_id in allowed_server_ids_for_job(job)
        if server_id and server_id != POOL_SERVER_ID
    }
    if job.assigned_server_id and job.assigned_server_id != POOL_SERVER_ID:
        server_ids.add(job.assigned_server_id)
    assigned_shard_servers = session.execute(
        select(WorkShard.assigned_server_id)
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.assigned_server_id.is_not(None))
    ).scalars().all()
    server_ids.update(str(server_id) for server_id in assigned_shard_servers if server_id)
    return server_ids

def _job_worker_version_summary(session: Session, job: Job) -> dict[str, Any]:
    versions = _server_versions(session, _job_worker_server_ids(session, job))
    if not versions:
        return {
            "worker_version_status": "unknown",
            "worker_version_warning": None,
            "worker_version_refs": {},
        }
    if len(versions) == 1:
        return {
            "worker_version_status": "consistent",
            "worker_version_warning": None,
            "worker_version_refs": versions,
        }
    return {
        "worker_version_status": "mixed",
        "worker_version_warning": "assigned workers report different git_ref or script_version values",
        "worker_version_refs": versions,
    }

def _resource_constrained_workers(session: Session, server_ids: set[str]) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    constrained: list[dict[str, Any]] = []
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        pressure = capabilities.get("resource_pressure")
        if not isinstance(pressure, dict) or not pressure.get("constrained"):
            continue
        reasons = pressure.get("reasons")
        constrained.append(
            {
                "server_id": server.id,
                "level": str(pressure.get("level") or "constrained"),
                "reasons": [str(item) for item in reasons] if isinstance(reasons, list) else [],
            }
        )
    return constrained

def _nonnegative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)

def _workers_with_event_spool_backlog(session: Session, server_ids: set[str]) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    workers: list[dict[str, Any]] = []
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        spool = capabilities.get("event_spool")
        if not isinstance(spool, dict):
            continue
        pending_events = _nonnegative_int(spool.get("pending_events"))
        pending_logs = _nonnegative_int(spool.get("pending_logs"))
        failed_events = _nonnegative_int(spool.get("failed_events"))
        failed_logs = _nonnegative_int(spool.get("failed_logs"))
        dropped_events = _nonnegative_int(spool.get("dropped_events"))
        dropped_logs = _nonnegative_int(spool.get("dropped_logs"))
        total_backlog = (
            pending_events
            + pending_logs
            + failed_events
            + failed_logs
            + dropped_events
            + dropped_logs
        )
        if total_backlog <= 0:
            continue
        workers.append(
            {
                "server_id": server.id,
                "dir": str(spool.get("dir") or ""),
                "pending_events": pending_events,
                "pending_logs": pending_logs,
                "failed_events": failed_events,
                "failed_logs": failed_logs,
                "dropped_events": dropped_events,
                "dropped_logs": dropped_logs,
                "total_backlog": total_backlog,
            }
        )
    return workers

def _workers_with_pending_shard_update_backlog(session: Session, server_ids: set[str]) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    workers: list[dict[str, Any]] = []
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        pending_updates = capabilities.get("pending_shard_updates")
        if not isinstance(pending_updates, dict):
            continue
        pending = _nonnegative_int(pending_updates.get("pending"))
        failed = _nonnegative_int(pending_updates.get("failed"))
        total_backlog = pending + failed
        if total_backlog <= 0:
            continue
        workers.append(
            {
                "server_id": server.id,
                "pending": pending,
                "failed": failed,
                "total_backlog": total_backlog,
            }
        )
    return workers

def preflight_job(session: Session, request: JobCreateRequest) -> JobPreflightResponse:
    ensure_pool_server(session)
    ensure_default_model_profiles(session)
    issues: list[JobPreflightIssue] = []
    database_status = database.describe_database_status(session.get_bind())
    database_dialect = str(database_status.get("dialect") or session.get_bind().dialect.name)
    if database_dialect != "postgresql":
        issues.append(
            _preflight_issue(
                "warning",
                "database_not_postgres",
                "Production jobs should use PostgreSQL; SQLite is for local development.",
                dialect=database_dialect,
                require_postgres=_env_truthy("OCR_PLATFORM_REQUIRE_POSTGRES"),
            )
        )
    migration_issue = _database_migration_preflight_issue(database_status)
    if migration_issue is not None:
        issues.append(migration_issue)
    auth_issue = _control_api_auth_preflight_issue()
    if auth_issue is not None:
        issues.append(auth_issue)

    allowed_ids = set(request.allowed_server_ids or [])
    if request.assigned_server_id:
        allowed_ids.add(request.assigned_server_id)
    eligibilities = [
        item
        for item in list_server_eligibility(session, request.input_dir)
        if item["server_id"] != POOL_SERVER_ID
        and (not allowed_ids or item["server_id"] in allowed_ids)
    ]
    eligible = [item for item in eligibilities if item.get("can_access")]
    ready = [item for item in eligible if item.get("status") in {"online", "idle"} and not item.get("is_stale")]
    if not eligible:
        issues.append(
            _preflight_issue(
                "error",
                "no_eligible_workers",
                "No selected worker can read the input shared path.",
                input_dir=request.input_dir,
            )
        )
    eligible_ids = {str(item["server_id"]) for item in eligible}

    def writable_workers_for(path: str) -> list[dict[str, Any]]:
        checks = [
            evaluate_server_path_access(server, path, require_writable=True)
            for server in list_servers(session)
            if server.id in eligible_ids
        ]
        return [item for item in checks if item.get("can_access")]

    output_writers = {str(item["server_id"]) for item in writable_workers_for(request.output_dir)}
    missing_output_writers = sorted(eligible_ids - output_writers)
    if missing_output_writers:
        issues.append(
            _preflight_issue(
                "error",
                "output_path_not_writable",
                "One or more eligible workers cannot confirm write access to output_dir.",
                path=request.output_dir,
                eligible_workers=sorted(eligible_ids),
                writable_workers=sorted(output_writers),
                unwritable_workers=missing_output_writers,
            )
        )
    effective_manifest_root = request.manifest_root or infer_default_manifest_root(
        session,
        input_dir=request.input_dir,
        input_mode=request.input_mode,
        assigned_server_id=request.assigned_server_id,
        allowed_server_ids=request.allowed_server_ids,
    )
    if effective_manifest_root:
        manifest_writers = {str(item["server_id"]) for item in writable_workers_for(effective_manifest_root)}
        missing_manifest_writers = sorted(eligible_ids - manifest_writers)
        if missing_manifest_writers:
            issues.append(
                _preflight_issue(
                    "error",
                    "manifest_root_not_writable",
                    "One or more eligible workers cannot confirm write access to manifest_root.",
                    path=effective_manifest_root,
                    inferred=not bool(request.manifest_root),
                    eligible_workers=sorted(eligible_ids),
                    writable_workers=sorted(manifest_writers),
                    unwritable_workers=missing_manifest_writers,
                )
            )
    versions = _server_versions(session, {str(item["server_id"]) for item in eligible})
    if len(versions) > 1:
        issues.append(
            _preflight_issue(
                "warning",
                "mixed_worker_versions",
                "Selected eligible workers report different git_ref or script_version values.",
                versions=versions,
            )
        )
    constrained_workers = _resource_constrained_workers(session, eligible_ids)
    if constrained_workers:
        issues.append(
            _preflight_issue(
                "warning",
                "resource_constrained_workers",
                "One or more eligible workers currently report resource pressure and may delay claiming work.",
                workers=constrained_workers,
            )
        )
    backlog_workers = _workers_with_event_spool_backlog(session, eligible_ids)
    if backlog_workers:
        issues.append(
            _preflight_issue(
                "warning",
                "worker_event_spool_backlog",
                "One or more eligible workers report unreplayed, quarantined, or dropped local event/log spool records.",
                workers=backlog_workers,
            )
        )
    pending_update_workers = _workers_with_pending_shard_update_backlog(session, eligible_ids)
    if pending_update_workers:
        issues.append(
            _preflight_issue(
                "warning",
                "worker_pending_shard_update_backlog",
                "One or more eligible workers report unreplayed or quarantined local shard progress updates.",
                workers=pending_update_workers,
            )
        )

    if request.model_profile_id:
        profile = session.get(ModelProfile, request.model_profile_id)
        if profile is None:
            issues.append(
                _preflight_issue(
                    "error",
                    "unknown_model_profile",
                    "Selected model profile does not exist.",
                    model_profile_id=request.model_profile_id,
                )
            )
        elif profile.requires_api_key and not (_resolve_model_profile_api_key(profile) or request.extra_args.get("api_key")):
            issues.append(
                _preflight_issue(
                    "error",
                    "model_profile_missing_api_key",
                    "Selected model profile requires an API key, but no saved or per-job key is available.",
                    model_profile_id=request.model_profile_id,
                )
            )
        elif profile.api_key:
            issues.append(
                _preflight_issue(
                    "warning",
                    "model_profile_saved_api_key",
                    "Selected model profile stores a legacy API key in the control database; save the profile with clear_api_key=true and migrate to api_key_env_var.",
                    model_profile_id=request.model_profile_id,
                    api_key_env_var=profile.api_key_env_var,
                )
            )

    if JOB_FILE_DETAIL_LIMIT > 100000 or JOB_EVENT_DETAIL_LIMIT > 100000:
        issues.append(
            _preflight_issue(
                "warning",
                "high_detail_row_limits",
                "Large per-file or raw-event retention limits can grow quickly on million-scale jobs.",
                job_file_detail_limit=JOB_FILE_DETAIL_LIMIT,
                job_event_detail_limit=JOB_EVENT_DETAIL_LIMIT,
            )
        )

    return JobPreflightResponse(
        ok=not any(issue.severity == "error" for issue in issues),
        database_dialect=database_dialect,
        total_workers=len(eligibilities),
        eligible_workers=len(eligible),
        ready_workers=len(ready),
        issues=issues,
    )

def claim_next_job(session: Session, server_id: str) -> Job | None:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return None

    select_stmt = (
        select(Job)
        .where(Job.assigned_server_id == server_id)
        .where(Job.status == "queued")
        .order_by(Job.created_at)
        .limit(1)
    )
    job = session.execute(select_stmt).scalar_one_or_none()
    if job is None:
        return claim_next_pool_job(session, server_id)

    started_at = utcnow()
    claim_stmt = (
        update(Job)
        .where(Job.id == job.id)
        .where(Job.status == "queued")
        .values(status="running", started_at=started_at)
    )
    result = session.execute(claim_stmt)
    if result.rowcount != 1:
        session.rollback()
        return claim_next_job(session, server_id)

    session.commit()
    return session.get(Job, job.id)

def _pool_job_has_claimable_shards(session: Session, job_id: str, now: datetime) -> bool:
    reconcile_expired_shard_leases(session, now=now, job_id=job_id)
    claimable_count = session.execute(
        select(func.count(WorkShard.id))
        .where(WorkShard.job_id == job_id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
    ).scalar_one()
    return bool(claimable_count)

def claim_next_pool_job(session: Session, server_id: str) -> Job | None:
    now = utcnow()
    candidates = session.execute(
        select(Job)
        .where(Job.assigned_server_id == POOL_SERVER_ID)
        .where(Job.status.in_({"queued", "running"}))
        .order_by(Job.created_at)
    ).scalars().all()
    for job in candidates:
        if not server_is_allowed_for_job(job, server_id):
            continue
        if not server_can_access_input_dir(session, server_id, job.input_dir):
            continue
        if job.input_mode in REMOTE_STATIC_INPUT_MODES and job.status == "queued":
            claim_stmt = (
                update(Job)
                .where(Job.id == job.id)
                .where(Job.status == "queued")
                .values(status="running", started_at=now)
            )
            result = session.execute(claim_stmt)
            if result.rowcount != 1:
                session.rollback()
                return claim_next_job(session, server_id)
            session.commit()
            return session.get(Job, job.id)
        if not _pool_job_has_claimable_shards(session, job.id, now):
            continue
        if job.status == "queued":
            claim_stmt = (
                update(Job)
                .where(Job.id == job.id)
                .where(Job.status == "queued")
                .values(status="running", started_at=now)
            )
            result = session.execute(claim_stmt)
            if result.rowcount != 1:
                session.rollback()
                return claim_next_job(session, server_id)
            session.commit()
            return session.get(Job, job.id)
        return job
    return None

def archive_server(session: Session, server_id: str) -> None:
    if server_id == POOL_SERVER_ID:
        raise ServerArchiveError("The internal server pool cannot be archived.")
    server = session.get(Server, server_id)
    if server is None:
        raise UnknownServerError(f"Unknown server: {server_id}")
    if server.archived_at is not None:
        return
    if effective_server_status(server) != "offline":
        raise ServerArchiveError("Only offline or stale servers can be archived.")
    stop_assigned_queued_jobs_for_server(session, server_id)
    if count_open_jobs_for_server(session, server_id) > 0 or count_running_shards_for_server(session, server_id) > 0:
        raise ServerArchiveError("Server still has active work.")

    server.archived_at = utcnow()
    session.commit()

def list_servers(session: Session, *, include_archived: bool = False) -> list[Server]:
    stmt = select(Server).order_by(Server.id.asc())
    if not include_archived:
        stmt = stmt.where(Server.archived_at.is_(None))
    return list(session.execute(stmt).scalars().all())

__all__ = [name for name in globals() if not name.startswith("__")]
