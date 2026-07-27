"""Read-only operational diagnostics built from persisted Control evidence."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Mapping

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ocr_parser.contracts.observability import (
    failure_category_label,
    fallback_category_label,
    stage_label,
    status_label,
)

from ...limits import ControlLimits as __ControlLimits
from ...models import (
    Job,
    JobCounter,
    JobEvent,
    JobFile,
    Manifest,
    Server,
    ShardAttempt,
    WorkShard,
)
from ..common import (
    COMPLETED_FILE_STATUSES,
    JOB_EVENT_DETAIL_LIMIT,
    JOB_STATUS_FILTERS,
    PERSIST_JOB_EVENT_DETAILS,
    POOL_SERVER_ID,
    SERVER_STALE_AFTER_SECONDS,
    TERMINAL_JOB_STATUSES,
)


EVIDENCE_ROW_LIMIT = 10_000
THROUGHPUT_WINDOW = timedelta(minutes=60)
EXECUTABLE_JOB_STATUSES = tuple(
    sorted(JOB_STATUS_FILTERS - TERMINAL_JOB_STATUSES - {"stopping"})
)
SLOT_OCCUPYING_JOB_STATUSES = tuple(
    sorted(JOB_STATUS_FILTERS - TERMINAL_JOB_STATUSES)
)
READY_WORKER_STATUSES = frozenset({"idle", "online", "busy"})
MANIFEST_INTEGRITY_STATUSES = frozenset(
    {"pending", "running", "ok", "failed", "missing_manifest", "unknown", "other"}
)

RECOMMENDATION_CODES = frozenset(
    {
        "apply_pending_migrations",
        "restore_worker_heartbeat",
        "add_worker_capacity",
        "relieve_worker_resource_pressure",
        "replay_spool_backlog",
        "reclaim_stale_leases",
        "investigate_stage_failures",
        "review_fallback_usage",
        "rerun_manifest_integrity",
        "run_output_audit",
        "collect_more_throughput_samples",
    }
)
ALERT_DEFINITIONS = (
    ("artifact_records_missing", "warning", "run_output_audit"),
    ("event_spool_backlog", "warning", "replay_spool_backlog"),
    ("fallback_usage", "warning", "review_fallback_usage"),
    ("log_spool_backlog", "warning", "replay_spool_backlog"),
    ("manifest_integrity_attention", "warning", "rerun_manifest_integrity"),
    ("migration_drift", "error", "apply_pending_migrations"),
    ("output_audit_not_reported", "warning", "run_output_audit"),
    ("shard_update_spool_backlog", "warning", "replay_spool_backlog"),
    ("stage_failures", "error", "investigate_stage_failures"),
    ("stale_shard_lease", "warning", "reclaim_stale_leases"),
    ("stale_worker_heartbeat", "warning", "restore_worker_heartbeat"),
    (
        "throughput_samples_insufficient",
        "warning",
        "collect_more_throughput_samples",
    ),
    ("worker_capacity_exhausted", "warning", "add_worker_capacity"),
    (
        "worker_resource_pressure",
        "warning",
        "relieve_worker_resource_pressure",
    ),
)
ALERT_BY_CODE = {
    code: (severity, recommendation)
    for code, severity, recommendation in ALERT_DEFINITIONS
}


class EvidenceLimitExceeded(RuntimeError):
    pass


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(timezone.utc)
    return (
        current.replace(tzinfo=timezone.utc)
        if current.tzinfo is None
        else current.astimezone(timezone.utc)
    )


def _nonnegative_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _resolve_evidence_limit(
    limits: __ControlLimits | None,
) -> int:
    if limits is None:
        return EVIDENCE_ROW_LIMIT
    return max(0, limits.diagnostics_evidence_row_limit)


def _resolve_event_retention(
    limits: __ControlLimits | None,
) -> tuple[bool, int]:
    if limits is None:
        return PERSIST_JOB_EVENT_DETAILS, JOB_EVENT_DETAIL_LIMIT
    return (
        limits.persist_job_event_details,
        limits.job_event_detail_limit,
    )


def _bounded_rows(rows, *, evidence_limit: int):
    values = list(rows)
    if len(values) > evidence_limit:
        raise EvidenceLimitExceeded("operational evidence row limit exceeded")
    return values


def _safe_json_object(payload: object) -> dict[str, object]:
    if not isinstance(payload, str):
        return {}
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _has_writable_shared_path(capabilities: Mapping[str, object]) -> bool:
    shared_paths = capabilities.get("shared_paths")
    if isinstance(shared_paths, list):
        for item in shared_paths:
            if (
                isinstance(item, Mapping)
                and item.get("exists") is True
                and item.get("readable") is True
                and item.get("writable") is True
            ):
                return True
    shared_roots = capabilities.get("shared_roots")
    return isinstance(shared_roots, list) and any(
        isinstance(root, str) and bool(root.strip())
        for root in shared_roots
    )


def _resource_constrained(capabilities: Mapping[str, object]) -> bool:
    pressure = capabilities.get("resource_pressure")
    return isinstance(pressure, Mapping) and pressure.get("constrained") is True


def _heartbeat_is_stale(server: Server, now: datetime) -> bool:
    if server.last_heartbeat_at is None:
        return True
    return now - server.last_heartbeat_at > timedelta(
        seconds=SERVER_STALE_AFTER_SECONDS
    )


def _worker_rows(
    session: Session,
    *,
    evidence_limit: int,
):
    return _bounded_rows(
        session.execute(
            select(Server)
            .where(Server.archived_at.is_(None))
            .where(Server.id != POOL_SERVER_ID)
            .order_by(Server.id.asc())
            .limit(evidence_limit + 1)
        ).scalars(),
        evidence_limit=evidence_limit,
    )


def _running_shards_by_server(
    session: Session,
    *,
    evidence_limit: int,
) -> tuple[dict[str, int], int]:
    rows = _bounded_rows(
        session.execute(
            select(
                WorkShard.assigned_server_id,
                func.count(WorkShard.id),
            )
            .join(Job, Job.id == WorkShard.job_id)
            .where(Job.status.in_(SLOT_OCCUPYING_JOB_STATUSES))
            .where(WorkShard.status == "running")
            .group_by(WorkShard.assigned_server_id)
            .order_by(WorkShard.assigned_server_id.asc())
            .limit(evidence_limit + 1)
        ),
        evidence_limit=evidence_limit,
    )
    by_server = {
        str(server_id): _nonnegative_int(count)
        for server_id, count in rows
        if server_id is not None
    }
    return by_server, sum(_nonnegative_int(count) for _, count in rows)


def _queue_and_lease_counts(
    session: Session,
    *,
    now: datetime,
) -> tuple[int, int]:
    pending = session.execute(
        select(func.count(WorkShard.id))
        .join(Job, Job.id == WorkShard.job_id)
        .where(Job.status.in_(EXECUTABLE_JOB_STATUSES))
        .where(WorkShard.status.in_(("pending", "retrying", "stale")))
    ).scalar_one()
    stale_leases = session.execute(
        select(func.count(WorkShard.id))
        .join(Job, Job.id == WorkShard.job_id)
        .where(Job.status.in_(SLOT_OCCUPYING_JOB_STATUSES))
        .where(WorkShard.status == "running")
        .where(WorkShard.lease_expires_at.is_not(None))
        .where(WorkShard.lease_expires_at <= now)
    ).scalar_one()
    return _nonnegative_int(pending), _nonnegative_int(stale_leases)


def _throughput_evidence(
    session: Session,
    *,
    now: datetime,
    evidence_limit: int,
    persist_event_details: bool,
    event_detail_limit: int,
) -> tuple[int, bool]:
    rows = list(
        session.execute(
            select(
                JobEvent.job_id,
                JobEvent.file_path,
                JobEvent.page_no,
            )
            .where(JobEvent.event_type == "page_done")
            .where(JobEvent.created_at >= now - THROUGHPUT_WINDOW)
            .where(JobEvent.created_at <= now)
            .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
            .limit(evidence_limit + 1)
        )
    )
    truncated = len(rows) > evidence_limit
    keys: set[tuple[str, str, int]] = set()
    missing_key = False
    for job_id, file_path, page_no in rows[:evidence_limit]:
        if (
            not isinstance(job_id, str)
            or not job_id
            or not isinstance(file_path, str)
            or not file_path
            or not isinstance(page_no, int)
        ):
            missing_key = True
            continue
        keys.add((job_id, file_path, page_no))
    complete = (
        persist_event_details
        and not (
            0 < event_detail_limit <= evidence_limit
        )
        and not truncated
        and not missing_key
    )
    return len(keys), complete


def _remaining_pages(
    session: Session,
    *,
    evidence_limit: int,
) -> tuple[int | None, bool]:
    rows = _bounded_rows(
        session.execute(
            select(
                Job.id,
                JobCounter.total_pages,
                JobCounter.completed_pages,
                JobCounter.started_files,
                func.coalesce(func.sum(Manifest.file_count), 0),
            )
            .select_from(Job)
            .outerjoin(JobCounter, JobCounter.job_id == Job.id)
            .outerjoin(Manifest, Manifest.job_id == Job.id)
            .where(Job.status.in_(EXECUTABLE_JOB_STATUSES))
            .group_by(
                Job.id,
                JobCounter.total_pages,
                JobCounter.completed_pages,
                JobCounter.started_files,
            )
            .order_by(Job.id.asc())
            .limit(evidence_limit + 1)
        ),
        evidence_limit=evidence_limit,
    )
    if not rows:
        return None, False
    remaining = 0
    for _, total_pages, completed_pages, started_files, manifest_files in rows:
        total = _nonnegative_int(total_pages)
        completed = _nonnegative_int(completed_pages)
        started = _nonnegative_int(started_files)
        files = _nonnegative_int(manifest_files)
        if total == 0 or files == 0 or started < files:
            return None, False
        remaining += max(total - completed, 0)
    return remaining, True


def capacity_diagnostics(
    session: Session,
    *,
    now: datetime | None = None,
    limits: __ControlLimits | None = None,
) -> dict[str, object]:
    current_time = _utc(now)
    evidence_limit = _resolve_evidence_limit(limits)
    persist_event_details, event_detail_limit = (
        _resolve_event_retention(limits)
    )
    with session.no_autoflush:
        workers = _worker_rows(
            session,
            evidence_limit=evidence_limit,
        )
        running_by_server, running_shards = _running_shards_by_server(
            session,
            evidence_limit=evidence_limit,
        )
        queue_depth, stale_leases = _queue_and_lease_counts(
            session,
            now=current_time,
        )
        sample_pages, throughput_complete = _throughput_evidence(
            session,
            now=current_time,
            evidence_limit=evidence_limit,
            persist_event_details=persist_event_details,
            event_detail_limit=event_detail_limit,
        )
        remaining_pages, remaining_reliable = _remaining_pages(
            session,
            evidence_limit=evidence_limit,
        )

    ready_slots = 0
    available_slots = 0
    constrained_workers = 0
    for server in workers:
        capabilities = _safe_json_object(server.capabilities_json)
        constrained = _resource_constrained(capabilities)
        constrained_workers += int(constrained)
        ready = (
            server.status in READY_WORKER_STATUSES
            and not _heartbeat_is_stale(server, current_time)
            and _has_writable_shared_path(capabilities)
            and not constrained
        )
        if not ready:
            continue
        capacity = _nonnegative_int(server.capacity_slots)
        ready_slots += capacity
        available_slots += max(
            capacity - running_by_server.get(server.id, 0),
            0,
        )

    observed_pages_per_hour = float(sample_pages)
    confidence = "none"
    estimated_drain_seconds: int | None = None
    if (
        sample_pages >= 10
        and throughput_complete
        and remaining_reliable
        and remaining_pages is not None
        and observed_pages_per_hour > 0
    ):
        confidence = "medium" if sample_pages >= 100 else "low"
        estimated_drain_seconds = round(
            remaining_pages / observed_pages_per_hour * 3600
        )

    recommendations: set[str] = set()
    has_workload = queue_depth > 0 or (
        remaining_reliable
        and remaining_pages is not None
        and remaining_pages > 0
    )
    if has_workload and (
        ready_slots == 0
        or (queue_depth > 0 and available_slots == 0)
    ):
        recommendations.add("add_worker_capacity")
    if constrained_workers:
        recommendations.add("relieve_worker_resource_pressure")
    if stale_leases:
        recommendations.add("reclaim_stale_leases")
    if has_workload and confidence == "none":
        recommendations.add("collect_more_throughput_samples")

    return {
        "available": True,
        "ready_worker_slots": ready_slots,
        "available_worker_slots": available_slots,
        "pending_shard_queue_depth": queue_depth,
        "running_shards": running_shards,
        "stale_leases": stale_leases,
        "observed_pages_per_hour": observed_pages_per_hour,
        "sample_pages": sample_pages,
        "estimated_drain_seconds": estimated_drain_seconds,
        "confidence": confidence,
        "recommendation_codes": sorted(recommendations),
    }


def _bounded_manifest_status(value: object) -> str:
    normalized = str(value or "unknown").strip()
    return normalized if normalized in MANIFEST_INTEGRITY_STATUSES else "other"


def _trace_audit(
    session: Session,
    *,
    now: datetime,
    evidence_limit: int,
    persist_event_details: bool,
    event_detail_limit: int,
) -> dict[str, object]:
    rows = list(
        session.execute(
            select(JobEvent.payload_json)
            .where(JobEvent.event_type == "page_done")
            .where(JobEvent.created_at >= now - THROUGHPUT_WINDOW)
            .where(JobEvent.created_at <= now)
            .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
            .limit(evidence_limit + 1)
        ).scalars()
    )
    truncated = (
        not persist_event_details
        or 0 < event_detail_limit <= evidence_limit
        or len(rows) > evidence_limit
    )
    stage_statuses: Counter[str] = Counter()
    stage_failures: Counter[str] = Counter()
    fallbacks: Counter[str] = Counter()
    for payload_json in rows[:evidence_limit]:
        payload = _safe_json_object(payload_json)
        stages = payload.get("stages")
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, Mapping):
                    continue
                bounded_stage = stage_label(stage.get("stage"))
                bounded_status = status_label(stage.get("status"))
                stage_statuses[f"{bounded_stage}.{bounded_status}"] += 1
                if bounded_status == "failed":
                    stage_failures[
                        failure_category_label(
                            stage.get("failure_category")
                        )
                    ] += 1
        fallback = payload.get("fallback")
        if isinstance(fallback, Mapping) and fallback.get("used") is True:
            fallbacks[
                fallback_category_label(fallback.get("reason"))
            ] += 1
    return {
        "stage_status_counts": dict(sorted(stage_statuses.items())),
        "stage_failure_category_counts": dict(sorted(stage_failures.items())),
        "fallback_category_counts": dict(sorted(fallbacks.items())),
        "evidence_truncated": truncated,
    }


def audit_diagnostics(
    session: Session,
    *,
    now: datetime | None = None,
    limits: __ControlLimits | None = None,
) -> dict[str, object]:
    current_time = _utc(now)
    evidence_limit = _resolve_evidence_limit(limits)
    persist_event_details, event_detail_limit = (
        _resolve_event_retention(limits)
    )
    with session.no_autoflush:
        manifest_rows = _bounded_rows(
            session.execute(
                select(
                    Manifest.worker_integrity_status,
                    func.count(Manifest.id),
                    func.sum(
                        case(
                            (
                                Manifest.worker_integrity_report_json.is_(
                                    None
                                )
                                | Manifest.worker_integrity_report_json.in_(
                                    ("", "{}")
                                ),
                                0,
                            ),
                            else_=1,
                        )
                    ),
                )
                .group_by(Manifest.worker_integrity_status)
                .order_by(Manifest.worker_integrity_status.asc())
                .limit(evidence_limit + 1)
            ),
            evidence_limit=evidence_limit,
        )
        attempt_rows = _bounded_rows(
            session.execute(
                select(
                    ShardAttempt.status,
                    ShardAttempt.failure_category,
                    func.count(ShardAttempt.id),
                )
                .group_by(
                    ShardAttempt.status,
                    ShardAttempt.failure_category,
                )
                .order_by(
                    ShardAttempt.status.asc(),
                    ShardAttempt.failure_category.asc(),
                )
                .limit(evidence_limit + 1)
            ),
            evidence_limit=evidence_limit,
        )
        artifact_rows = _bounded_rows(
            session.execute(
                select(
                    JobFile.status,
                    func.count(JobFile.id),
                )
                .where(JobFile.output_path.is_not(None))
                .where(JobFile.output_path != "")
                .group_by(JobFile.status)
                .order_by(JobFile.status.asc())
                .limit(evidence_limit + 1)
            ),
            evidence_limit=evidence_limit,
        )
        missing_artifacts = session.execute(
            select(func.count(JobFile.id))
            .where(JobFile.status.in_(COMPLETED_FILE_STATUSES))
            .where(
                (JobFile.output_path.is_(None))
                | (JobFile.output_path == "")
            )
        ).scalar_one()
        completed_jobs = session.execute(
            select(func.count(Job.id)).where(Job.status == "succeeded")
        ).scalar_one()
        execution = _trace_audit(
            session,
            now=current_time,
            evidence_limit=evidence_limit,
            persist_event_details=persist_event_details,
            event_detail_limit=event_detail_limit,
        )

    manifest_statuses: Counter[str] = Counter()
    manifest_total = 0
    reports_present = 0
    for status, count, present in manifest_rows:
        bounded_count = _nonnegative_int(count)
        manifest_statuses[_bounded_manifest_status(status)] += bounded_count
        manifest_total += bounded_count
        reports_present += _nonnegative_int(present)

    attempt_statuses: Counter[str] = Counter()
    attempt_failures: Counter[str] = Counter()
    for status, failure, count in attempt_rows:
        bounded_count = _nonnegative_int(count)
        attempt_statuses[status_label(status)] += bounded_count
        if failure is not None:
            attempt_failures[
                failure_category_label(failure)
            ] += bounded_count

    artifact_statuses: Counter[str] = Counter()
    declared_artifacts = 0
    for status, count in artifact_rows:
        bounded_count = _nonnegative_int(count)
        artifact_statuses[status_label(status)] += bounded_count
        declared_artifacts += bounded_count

    return {
        "available": True,
        "manifest_integrity": {
            "status_counts": dict(sorted(manifest_statuses.items())),
            "reports_present": reports_present,
            "reports_missing": max(manifest_total - reports_present, 0),
        },
        "shard_attempts": {
            "status_counts": dict(sorted(attempt_statuses.items())),
            "failure_category_counts": dict(
                sorted(attempt_failures.items())
            ),
        },
        "execution": execution,
        "artifacts": {
            "declared_records": declared_artifacts,
            "status_counts": dict(sorted(artifact_statuses.items())),
            "missing_declared_records": _nonnegative_int(
                missing_artifacts
            ),
            "completed_jobs": _nonnegative_int(completed_jobs),
        },
        "output_audit": {
            "status": "not_reported",
            "evidence_available": False,
        },
    }


def _alert_item(code: str, count: object) -> dict[str, object]:
    severity, recommendation = ALERT_BY_CODE[code]
    if recommendation not in RECOMMENDATION_CODES:
        raise RuntimeError("unknown operational recommendation")
    return {
        "code": code,
        "severity": severity,
        "count": _nonnegative_int(count),
        "recommendation_code": recommendation,
    }


def alert_templates() -> list[dict[str, object]]:
    return [
        _alert_item(code, 0)
        for code, _, _ in ALERT_DEFINITIONS
    ]


def migration_alerts(
    database_status: Mapping[str, object],
) -> list[dict[str, object]]:
    mismatches = database_status.get("checksum_mismatches")
    missing_checksums = database_status.get("missing_checksums")
    missing_migrations = database_status.get("missing_migrations")
    unexpected_migrations = database_status.get("unexpected_migrations")
    count = (
        len(mismatches) if isinstance(mismatches, list) else 0
    ) + (
        len(missing_checksums) if isinstance(missing_checksums, list) else 0
    ) + (
        len(missing_migrations) if isinstance(missing_migrations, list) else 0
    ) + (
        len(unexpected_migrations)
        if isinstance(unexpected_migrations, list)
        else 0
    )
    if (
        not database_status.get("schema_migrations_table_exists")
        or not database_status.get("is_current")
    ):
        count = max(count, 1)
    return [_alert_item("migration_drift", count)] if count else []


def _worker_alert_counts(
    session: Session,
    *,
    now: datetime,
    evidence_limit: int,
) -> dict[str, int]:
    counts = {
        "stale_worker_heartbeat": 0,
        "worker_resource_pressure": 0,
        "event_spool_backlog": 0,
        "log_spool_backlog": 0,
        "shard_update_spool_backlog": 0,
    }
    for server in _worker_rows(
        session,
        evidence_limit=evidence_limit,
    ):
        capabilities = _safe_json_object(server.capabilities_json)
        counts["stale_worker_heartbeat"] += int(
            _heartbeat_is_stale(server, now)
        )
        counts["worker_resource_pressure"] += int(
            _resource_constrained(capabilities)
        )
        spool = capabilities.get("event_spool")
        if isinstance(spool, Mapping):
            counts["event_spool_backlog"] += sum(
                _nonnegative_int(spool.get(key))
                for key in (
                    "pending_events",
                    "failed_events",
                    "dropped_events",
                )
            )
            counts["log_spool_backlog"] += sum(
                _nonnegative_int(spool.get(key))
                for key in (
                    "pending_logs",
                    "failed_logs",
                    "dropped_logs",
                )
            )
        shard_updates = capabilities.get("pending_shard_updates")
        if isinstance(shard_updates, Mapping):
            counts["shard_update_spool_backlog"] += sum(
                _nonnegative_int(shard_updates.get(key))
                for key in ("pending", "failed")
            )
    return counts


def alerts_diagnostics(
    session: Session,
    *,
    database_status: Mapping[str, object],
    capacity: Mapping[str, object],
    audit: Mapping[str, object],
    now: datetime | None = None,
    limits: __ControlLimits | None = None,
) -> dict[str, object]:
    current_time = _utc(now)
    evidence_limit = _resolve_evidence_limit(limits)
    active = migration_alerts(database_status)
    with session.no_autoflush:
        worker_counts = _worker_alert_counts(
            session,
            now=current_time,
            evidence_limit=evidence_limit,
        )
    active.extend(
        _alert_item(code, count)
        for code, count in worker_counts.items()
        if count
    )

    if capacity.get("available") is True:
        queue_depth = _nonnegative_int(
            capacity.get("pending_shard_queue_depth")
        )
        available_slots = _nonnegative_int(
            capacity.get("available_worker_slots")
        )
        if queue_depth and available_slots == 0:
            active.append(
                _alert_item("worker_capacity_exhausted", queue_depth)
            )
        stale_leases = _nonnegative_int(capacity.get("stale_leases"))
        if stale_leases:
            active.append(
                _alert_item("stale_shard_lease", stale_leases)
            )
        sample_pages = _nonnegative_int(capacity.get("sample_pages"))
        if queue_depth and capacity.get("confidence") == "none":
            active.append(
                _alert_item(
                    "throughput_samples_insufficient",
                    max(10 - sample_pages, 1),
                )
            )

    if audit.get("available") is True:
        execution = audit.get("execution")
        if isinstance(execution, Mapping):
            stage_failures = execution.get(
                "stage_failure_category_counts"
            )
            if isinstance(stage_failures, Mapping):
                count = sum(
                    _nonnegative_int(value)
                    for value in stage_failures.values()
                )
                if count:
                    active.append(_alert_item("stage_failures", count))
            fallbacks = execution.get("fallback_category_counts")
            if isinstance(fallbacks, Mapping):
                count = sum(
                    _nonnegative_int(value)
                    for value in fallbacks.values()
                )
                if count:
                    active.append(_alert_item("fallback_usage", count))

        manifest = audit.get("manifest_integrity")
        if isinstance(manifest, Mapping):
            statuses = manifest.get("status_counts")
            count = 0
            if isinstance(statuses, Mapping):
                count = sum(
                    _nonnegative_int(statuses.get(status))
                    for status in ("failed", "missing_manifest", "other")
                )
            if count:
                active.append(
                    _alert_item("manifest_integrity_attention", count)
                )

        artifacts = audit.get("artifacts")
        if isinstance(artifacts, Mapping):
            count = _nonnegative_int(
                artifacts.get("missing_declared_records")
            )
            if count:
                active.append(
                    _alert_item("artifact_records_missing", count)
                )

        output_audit = audit.get("output_audit")
        artifacts = audit.get("artifacts")
        output_evidence_count = 0
        if isinstance(artifacts, Mapping):
            output_evidence_count = sum(
                _nonnegative_int(artifacts.get(key))
                for key in (
                    "declared_records",
                    "missing_declared_records",
                    "completed_jobs",
                )
            )
        if (
            output_evidence_count
            and isinstance(output_audit, Mapping)
            and output_audit.get("evidence_available") is not True
        ):
            active.append(_alert_item("output_audit_not_reported", 1))

    return {
        "available": True,
        "active": sorted(active, key=lambda item: str(item["code"])),
        "templates": alert_templates(),
    }


def unavailable_section(name: str) -> dict[str, object]:
    return {
        "available": False,
        "code": f"{name}_diagnostics_unavailable",
    }


def unavailable_alerts(
    database_status: Mapping[str, object],
) -> dict[str, object]:
    return {
        "available": False,
        "code": "alerts_diagnostics_unavailable",
        "active": migration_alerts(database_status),
        "templates": alert_templates(),
    }


__all__ = [
    "ALERT_DEFINITIONS",
    "EVIDENCE_ROW_LIMIT",
    "EXECUTABLE_JOB_STATUSES",
    "RECOMMENDATION_CODES",
    "SLOT_OCCUPYING_JOB_STATUSES",
    "THROUGHPUT_WINDOW",
    "alert_templates",
    "alerts_diagnostics",
    "audit_diagnostics",
    "capacity_diagnostics",
    "migration_alerts",
    "unavailable_alerts",
    "unavailable_section",
]
