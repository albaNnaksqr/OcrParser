"""Neutral scheduling ownership for shard lease recovery and finalization."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from .domains.common import (
    RECLAIMABLE_SHARD_STATUSES,
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


def _commit_reconciliation(session):
    session.commit()


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
