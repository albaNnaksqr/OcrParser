"""Worker-to-job assignment coordination."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import scheduling
from ...models import Job, Server, WorkShard
from ..common import (
    POOL_SERVER_ID,
    RECLAIMABLE_SHARD_STATUSES,
    REMOTE_STATIC_INPUT_MODES,
    utcnow,
)
from ..jobs import policy as job_policy
from .eligibility import server_can_access_input_dir
from .identity import server_is_allowed_for_job


class JobClaimCollision(RuntimeError):
    """The selected queued job was claimed by another worker."""


def pool_job_has_claimable_shards(
    session: Session,
    job_id: str,
    now: datetime,
) -> bool:
    scheduling.reconcile_expired_shard_leases(
        session,
        now=now,
        job_id=job_id,
    )
    claimable_count = session.execute(
        select(func.count(WorkShard.id))
        .join(Job, Job.id == WorkShard.job_id)
        .where(WorkShard.job_id == job_id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
        .where(WorkShard.attempt_count < Job.max_shard_attempts)
    ).scalar_one()
    return bool(claimable_count)


def _claim_queued_job(
    session: Session,
    job: Job,
    *,
    started_at: datetime,
) -> Job:
    if not job_policy.claim_queued(
        session,
        job.id,
        started_at=started_at,
    ):
        raise JobClaimCollision(job.id)
    session.flush()
    return session.get(Job, job.id)


def claim_next_pool_job(
    session: Session,
    server_id: str,
) -> Job | None:
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
        if not server_can_access_input_dir(
            session,
            server_id,
            job.input_dir,
        ):
            continue
        if (
            job.input_mode in REMOTE_STATIC_INPUT_MODES
            and job.status == "queued"
        ):
            return _claim_queued_job(
                session,
                job,
                started_at=now,
            )
        if not pool_job_has_claimable_shards(
            session,
            job.id,
            now,
        ):
            continue
        if job.status == "queued":
            return _claim_queued_job(
                session,
                job,
                started_at=now,
            )
        return job
    return None


def claim_next_job(
    session: Session,
    server_id: str,
) -> Job | None:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return None

    job = session.execute(
        select(Job)
        .where(Job.assigned_server_id == server_id)
        .where(Job.status == "queued")
        .order_by(Job.created_at)
        .limit(1)
    ).scalar_one_or_none()
    if job is not None:
        return _claim_queued_job(
            session,
            job,
            started_at=utcnow(),
        )

    running_jobs = session.execute(
        select(Job)
        .where(Job.assigned_server_id == server_id)
        .where(Job.status == "running")
        .order_by(Job.created_at)
    ).scalars().all()
    for running_job in running_jobs:
        if pool_job_has_claimable_shards(
            session,
            running_job.id,
            utcnow(),
        ):
            return running_job
    return claim_next_pool_job(session, server_id)


__all__ = [
    "JobClaimCollision",
    "claim_next_job",
    "claim_next_pool_job",
    "pool_job_has_claimable_shards",
]
