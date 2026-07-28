"""Neutral scheduling ownership for shard lease recovery and finalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from ocr_parser.infra.failure_category import infer_failure_category
from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from .domains.common import (
    RECLAIMABLE_SHARD_STATUSES,
    ScanUnitAttemptConflictError,
    SHARD_LEASE_SECONDS,
    ShardAttemptConflictError,
    TERMINAL_JOB_STATUSES,
    TERMINAL_SHARD_STATUSES,
)
from .models import Job, ScanUnit, ShardAttempt, WorkShard, utcnow


_SHARD_LEASE_ATTEMPTS_EXHAUSTED_ERROR = (
    "shard lease expired after maximum attempts"
)


@dataclass(frozen=True)
class _ShardLeaseReconcileResult:
    changed: bool = False
    exhausted_shard_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class ScanUnitTransitionPlan:
    unit: ScanUnit
    should_apply: bool


@dataclass(frozen=True)
class WorkShardUpdateSnapshot:
    job_id: str
    status: str


class WorkShardUpdateData(Protocol):
    status: str
    assigned_server_id: str | None
    attempt_count: int | None
    processed_files: int
    failed_files: int
    skipped_files: int
    completed_pages: int
    api_inflight: int | None
    api_inflight_peak: int | None
    api_waiting: int | None
    oldest_api_inflight: float | None
    execution_paused: bool | None
    api_concurrency_limit: int | None
    execution_control_reason: str | None
    failure_category: str | None
    error_message: str | None


def _scan_unit_transition_plan(
    unit: ScanUnit,
    *,
    assigned_server_id: str | None,
    attempt_count: int | None,
    terminal_status: str,
    operation: str,
) -> ScanUnitTransitionPlan:
    if (
        assigned_server_id is not None
        and assigned_server_id != unit.assigned_server_id
    ):
        raise ScanUnitAttemptConflictError(
            f"scan unit {operation} belongs to a different server attempt"
        )
    if attempt_count is not None and attempt_count != unit.attempt_count:
        raise ScanUnitAttemptConflictError(
            f"scan unit {operation} belongs to a stale attempt"
        )
    if unit.status == terminal_status:
        return ScanUnitTransitionPlan(unit=unit, should_apply=False)
    if unit.status != "running":
        raise ScanUnitAttemptConflictError(
            f"scan unit is not running: {unit.status}"
        )
    return ScanUnitTransitionPlan(unit=unit, should_apply=True)


def _lock_scan_unit_for_transition(
    session: Session,
    scan_unit_id: int,
) -> ScanUnit:
    unit = session.execute(
        select(ScanUnit)
        .where(ScanUnit.id == scan_unit_id)
        .with_for_update()
    ).scalar_one_or_none()
    if unit is None:
        raise ValueError(f"unknown scan unit: {scan_unit_id}")
    return unit


def plan_scan_unit_completion(
    session: Session,
    scan_unit_id: int,
    *,
    assigned_server_id: str | None,
    attempt_count: int | None,
) -> ScanUnitTransitionPlan:
    return _scan_unit_transition_plan(
        _lock_scan_unit_for_transition(session, scan_unit_id),
        assigned_server_id=assigned_server_id,
        attempt_count=attempt_count,
        terminal_status="succeeded",
        operation="completion",
    )


def apply_scan_unit_completion(
    plan: ScanUnitTransitionPlan,
    *,
    manifest_path: str | None,
    meta_path: str | None,
    file_count: int,
    total_bytes: int,
    finished_at: datetime,
) -> ScanUnit:
    unit = plan.unit
    if not plan.should_apply:
        return unit
    unit.status = "succeeded"
    unit.manifest_path = manifest_path
    unit.meta_path = meta_path
    unit.file_count = file_count
    unit.total_bytes = total_bytes
    unit.finished_at = finished_at
    unit.lease_expires_at = None
    return unit


def plan_scan_unit_failure(
    session: Session,
    scan_unit_id: int,
    *,
    assigned_server_id: str | None,
    attempt_count: int | None,
) -> ScanUnitTransitionPlan:
    return _scan_unit_transition_plan(
        _lock_scan_unit_for_transition(session, scan_unit_id),
        assigned_server_id=assigned_server_id,
        attempt_count=attempt_count,
        terminal_status="failed",
        operation="failure",
    )


def apply_scan_unit_failure(
    plan: ScanUnitTransitionPlan,
    *,
    failure_category: str | None,
    error_message: str,
    finished_at: datetime,
) -> ScanUnit:
    unit = plan.unit
    if not plan.should_apply:
        return unit
    unit.status = "failed"
    unit.failure_category = failure_category
    unit.error_message = error_message
    unit.finished_at = finished_at
    unit.lease_expires_at = None
    return unit


def new_pending_scan_unit(*, job_id: str, path: str) -> ScanUnit:
    return ScanUnit(job_id=job_id, path=path, status="pending")


def new_pending_work_shard(
    *,
    job_id: str,
    manifest_id: int,
    shard_index: int,
    shard_path: str,
    file_count: int,
) -> WorkShard:
    return WorkShard(
        job_id=job_id,
        manifest_id=manifest_id,
        shard_index=shard_index,
        shard_path=shard_path,
        status="pending",
        file_count=file_count,
    )


def shard_lease_deadline(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=SHARD_LEASE_SECONDS)


def scan_unit_lease_deadline(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=SHARD_LEASE_SECONDS)


def renew_running_shard_leases(
    session: Session,
    server_id: str,
    *,
    job_id: str,
    now: datetime,
) -> None:
    session.execute(
        update(WorkShard)
        .where(WorkShard.assigned_server_id == server_id)
        .where(WorkShard.job_id == job_id)
        .where(WorkShard.status == "running")
        .where(WorkShard.lease_expires_at.is_not(None))
        .where(WorkShard.lease_expires_at > now)
        .values(lease_expires_at=shard_lease_deadline(now))
    )


def renew_running_scan_unit_leases(
    session: Session,
    server_id: str,
    *,
    job_id: str,
    now: datetime,
) -> None:
    session.execute(
        update(ScanUnit)
        .where(ScanUnit.assigned_server_id == server_id)
        .where(ScanUnit.job_id == job_id)
        .where(ScanUnit.status == "running")
        .where(ScanUnit.lease_expires_at.is_not(None))
        .where(ScanUnit.lease_expires_at > now)
        .values(lease_expires_at=scan_unit_lease_deadline(now))
    )


def reconcile_expired_scan_unit_leases(
    session: Session,
    *,
    now: datetime | None = None,
    job_id: str | None = None,
) -> None:
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


def _expired_running_shard_filter(now: datetime):
    return (
        (WorkShard.status == "running")
        & (WorkShard.lease_expires_at.is_not(None))
        & (WorkShard.lease_expires_at <= now)
    )


def _latest_current_shard_attempt(
    session: Session,
    shard: WorkShard,
    *,
    for_update: bool = False,
) -> ShardAttempt | None:
    statement = (
        select(ShardAttempt)
        .where(ShardAttempt.shard_id == shard.id)
        .where(ShardAttempt.attempt_number == shard.attempt_count)
        .order_by(ShardAttempt.id.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return session.execute(statement).scalar_one_or_none()


def get_work_shard_update_snapshot(
    session: Session,
    shard_id: int,
) -> WorkShardUpdateSnapshot:
    snapshot = session.execute(
        select(WorkShard.job_id, WorkShard.status)
        .where(WorkShard.id == shard_id)
    ).one_or_none()
    if snapshot is None:
        raise ValueError(f"unknown shard: {shard_id}")
    job_id, status = snapshot
    return WorkShardUpdateSnapshot(job_id=job_id, status=status)


def work_shard_update_requires_job_lock(
    *,
    requested_status: str,
    observed_status: str,
) -> bool:
    return (
        requested_status in TERMINAL_SHARD_STATUSES
        or observed_status in TERMINAL_SHARD_STATUSES
    )


def lock_work_shard_for_update(
    session: Session,
    shard_id: int,
) -> WorkShard:
    shard = session.execute(
        select(WorkShard)
        .where(WorkShard.id == shard_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if shard is None:
        raise ValueError(f"unknown shard: {shard_id}")
    return shard


def _remaining_retry_status(job: Job, shard: WorkShard) -> str:
    return (
        "retrying"
        if shard.attempt_count < job.max_shard_attempts
        else "failed"
    )


def apply_work_shard_update(
    session: Session,
    *,
    shard: WorkShard,
    job: Job | None,
    request: WorkShardUpdateData,
) -> WorkShard:
    if (
        request.assigned_server_id is not None
        and request.assigned_server_id != shard.assigned_server_id
    ):
        raise ShardAttemptConflictError(
            "shard update belongs to a different server attempt"
        )
    if (
        request.attempt_count is not None
        and request.attempt_count != shard.attempt_count
    ):
        raise ShardAttemptConflictError(
            "shard update belongs to a stale attempt"
        )
    if shard.status in TERMINAL_SHARD_STATUSES:
        if job is not None:
            _finalize_job_after_shard_change(
                session,
                job,
                now=utcnow(),
            )
        return shard
    if (
        shard.status in {"retrying", "stale"}
        and request.status not in TERMINAL_SHARD_STATUSES
    ):
        return shard

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
        if job is None:
            raise RuntimeError(
                "failed shard update requires Job serialization"
            )
        shard.status = _remaining_retry_status(job, shard)

    if shard.status in TERMINAL_SHARD_STATUSES:
        shard.finished_at = utcnow()
        shard.lease_expires_at = None
    elif shard.status in {"retrying", "stale"}:
        shard.finished_at = None
        shard.lease_expires_at = None

    attempt = _latest_current_shard_attempt(session, shard)
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

    if shard.status in TERMINAL_SHARD_STATUSES:
        if job is None:
            raise RuntimeError(
                "terminal shard update requires Job serialization"
            )
        _finalize_job_after_shard_change(
            session,
            job,
            now=utcnow(),
            preferred_failure_shard_id=(
                shard.id if shard.status == "failed" else None
            ),
        )
    return shard


def _lock_job_for_shard_change(
    session: Session,
    job_id: str,
) -> Job | None:
    return session.execute(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _deterministic_failed_shard(
    session: Session,
    job_id: str,
    *,
    preferred_shard_id: int | None,
) -> WorkShard | None:
    if preferred_shard_id is not None:
        preferred = session.execute(
            select(WorkShard)
            .where(WorkShard.id == preferred_shard_id)
            .where(WorkShard.job_id == job_id)
            .where(WorkShard.status == "failed")
        ).scalar_one_or_none()
        if preferred is not None:
            return preferred
    return session.execute(
        select(WorkShard)
        .where(WorkShard.job_id == job_id)
        .where(WorkShard.status == "failed")
        .order_by(
            case((WorkShard.finished_at.is_(None), 1), else_=0).asc(),
            WorkShard.finished_at.desc(),
            WorkShard.shard_index.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()


def _finalize_job_after_shard_change(
    session: Session,
    job: Job,
    *,
    now: datetime,
    preferred_failure_shard_id: int | None = None,
) -> bool:
    """Finalize a Job while its row lock serializes shard terminal paths."""

    session.flush()
    if job.status in TERMINAL_JOB_STATUSES:
        return False
    open_shards = session.execute(
        select(func.count(WorkShard.id))
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.status.not_in(TERMINAL_SHARD_STATUSES))
    ).scalar_one()
    if int(open_shards or 0) > 0:
        return False

    if job.stop_requested or job.status == "stopping":
        open_scan_units = session.execute(
            select(func.count(ScanUnit.id))
            .where(ScanUnit.job_id == job.id)
            .where(ScanUnit.status.in_({"pending", "running", "stale"}))
        ).scalar_one()
        if int(open_scan_units or 0) > 0:
            return False
        job.status = "stopped"
        if job.failure_category is None:
            job.failure_category = "operator_stopped"
        if job.finished_at is None:
            job.finished_at = now
        return True

    failed_shard = _deterministic_failed_shard(
        session,
        job.id,
        preferred_shard_id=preferred_failure_shard_id,
    )
    if failed_shard is None:
        return False
    job.status = "failed"
    job.failure_category = failed_shard.failure_category or "shard_failed"
    job.error_message = failed_shard.error_message
    job.finished_at = now
    return True


def _reconcile_expired_shard_leases(
    session: Session,
    *,
    now: datetime | None = None,
    job_id: str | None = None,
) -> _ShardLeaseReconcileResult:
    current_time = now or utcnow()
    legacy_exhausted_filter = (
        WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES)
        & (WorkShard.attempt_count >= Job.max_shard_attempts)
    )
    candidate_job_ids = (
        select(WorkShard.job_id)
        .join(Job, Job.id == WorkShard.job_id)
        .where(
            _expired_running_shard_filter(current_time)
            | legacy_exhausted_filter
        )
        .distinct()
        .order_by(WorkShard.job_id.asc())
    )
    if job_id is not None:
        candidate_job_ids = candidate_job_ids.where(WorkShard.job_id == job_id)
    job_ids = list(session.execute(candidate_job_ids).scalars())
    if not job_ids:
        return _ShardLeaseReconcileResult()

    exhausted_shard_ids: list[int] = []
    changed = False
    for candidate_job_id in job_ids:
        job = _lock_job_for_shard_change(session, candidate_job_id)
        if job is None:
            continue
        candidate_shards = list(
            session.execute(
                select(WorkShard)
                .where(WorkShard.job_id == candidate_job_id)
                .where(
                    _expired_running_shard_filter(current_time)
                    | (
                        WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES)
                        & (
                            WorkShard.attempt_count
                            >= job.max_shard_attempts
                        )
                    )
                )
                .order_by(WorkShard.id.asc())
                .with_for_update()
                .execution_options(populate_existing=True)
            ).scalars()
        )
        if not candidate_shards:
            continue
        changed = True
        job_exhausted_ids: list[int] = []
        for shard in candidate_shards:
            attempt = _latest_current_shard_attempt(
                session,
                shard,
                for_update=True,
            )
            if job.stop_requested or job.status == "stopping":
                if attempt is not None and attempt.status not in {
                    "succeeded",
                    "failed",
                    "stopped",
                }:
                    attempt.status = "stopped"
                    attempt.failure_category = "operator_stopped"
                    attempt.finished_at = current_time
                shard.status = "stopped"
                shard.failure_category = "operator_stopped"
                shard.lease_expires_at = None
                shard.finished_at = current_time
                continue

            if shard.attempt_count >= job.max_shard_attempts:
                if attempt is not None and attempt.status not in {
                    "succeeded",
                    "failed",
                    "stopped",
                }:
                    attempt.status = "failed"
                    attempt.failure_category = "lease_expired"
                    attempt.error_message = (
                        _SHARD_LEASE_ATTEMPTS_EXHAUSTED_ERROR
                    )
                    attempt.finished_at = current_time
                shard.status = "failed"
                shard.failure_category = "lease_expired"
                shard.error_message = _SHARD_LEASE_ATTEMPTS_EXHAUSTED_ERROR
                shard.lease_expires_at = None
                shard.finished_at = current_time
                exhausted_shard_ids.append(shard.id)
                job_exhausted_ids.append(shard.id)
                continue

            if attempt is not None and attempt.status == "running":
                attempt.status = "stale"
                attempt.failure_category = "lease_expired"
                attempt.finished_at = current_time
            shard.status = "stale"
            shard.lease_expires_at = None

        _finalize_job_after_shard_change(
            session,
            job,
            now=current_time,
            preferred_failure_shard_id=(
                job_exhausted_ids[-1] if job_exhausted_ids else None
            ),
        )
    return _ShardLeaseReconcileResult(
        changed=changed,
        exhausted_shard_ids=tuple(exhausted_shard_ids),
    )


def reconcile_expired_shard_leases(
    session: Session,
    *,
    now: datetime | None = None,
    job_id: str | None = None,
) -> None:
    _reconcile_expired_shard_leases(session, now=now, job_id=job_id)


def _commit_reconciliation(session):
    session.commit()


def _flush_reconciliation(session):
    session.flush()


def _flush_finalization(session):
    session.flush()


def _claim_work_shard(
    session: Session,
    *,
    shard_id: int,
    job_id: str,
    server_id: str,
    started_at: datetime,
    lease_expires_at: datetime,
    reclaimable_statuses: set[str],
    non_claimable_job_statuses: set[str],
):
    claimable_parent = (
        select(Job.id)
        .where(Job.id == job_id)
        .where(Job.stop_requested.is_(False))
        .where(Job.status.not_in(non_claimable_job_statuses))
        .exists()
    )
    statement = (
        update(WorkShard)
        .where(WorkShard.id == shard_id)
        .where(WorkShard.status.in_(reclaimable_statuses))
        .where(
            WorkShard.attempt_count
            < select(Job.max_shard_attempts)
            .where(Job.id == WorkShard.job_id)
            .scalar_subquery()
        )
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
    return session.execute(statement), claimable_parent


def _fence_running_shards_for_restarted_server(
    session: Session,
    shard_ids: list[int],
) -> None:
    session.execute(
        update(WorkShard)
        .where(WorkShard.id.in_(shard_ids))
        .where(WorkShard.status == "running")
        .values(
            status="stale",
            assigned_server_id=None,
            failure_category="process_killed",
            error_message="worker process re-registered before shard completion",
            lease_expires_at=None,
            finished_at=None,
        )
    )
