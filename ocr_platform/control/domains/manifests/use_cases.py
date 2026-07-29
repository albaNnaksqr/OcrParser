"""Cross-domain Manifest application use cases.

Scheduling owns claim and transition algorithms; these use cases coordinate
those policies with Manifest construction, freeze, and path policy.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import scheduling as scheduling_policy
from ...limits import ControlLimits, legacy_control_limits
from ...models import Job, ScanUnit, Server, WorkShard
from ...schemas import ScanUnitCompleteRequest, ScanUnitFailRequest
from ..common import *
from ..jobs import policy as job_policy
from . import construction as manifest_ports
from .freeze import fail_manifest_if_scan_complete, freeze_manifest_if_scan_complete
from .paths import server_can_access_input_dir, server_is_allowed_for_job
from .projection import get_job_or_raise
def _claim_next_scan_unit_phase(
    session: Session,
    server_id: str,
    *,
    claim_statuses: set[str],
    now: datetime | None,
    reconcile: bool,
) -> tuple[ScanUnit | None, datetime | None, bool]:

    if reconcile:
        server = session.get(Server, server_id)
        if server is None or server.archived_at is not None:
            return None, None, False
        now = utcnow()
        scheduling_policy.reconcile_expired_scan_unit_leases(session, now=now)
    if now is None:
        raise RuntimeError("scan unit claim phase requires a claim timestamp")

    after_id: int | None = None
    while True:
        candidate_ids = session.execute(
            scheduling_policy._claimable_scan_unit_id_select(
                limit=SCAN_UNIT_CLAIM_BATCH_SIZE,
                after_id=after_id,
                statuses=claim_statuses,
            )
        ).scalars().all()
        if not candidate_ids:
            return None, now, True
        after_id = max(candidate_ids)
        for unit_id in candidate_ids:
            unit = session.get(ScanUnit, unit_id)
            if unit is None:
                continue
            job = unit.job
            if not server_is_allowed_for_job(job, server_id):
                continue
            if not server_can_access_input_dir(
                session,
                server_id,
                unit.path,
            ):
                continue
            scheduling_policy._claim_scan_unit_candidate(
                session,
                unit.id,
                server_id,
                claim_statuses=claim_statuses,
                now=now,
            )
            job_policy.start_if_queued(job, started_at=now)
            session.refresh(unit)
            return unit, now, True

def _complete_scan_unit(
    session: Session,
    scan_unit_id: int,
    request: ScanUnitCompleteRequest,
    *,
    limits: ControlLimits | None = None,
) -> ScanUnit:

    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    plan = scheduling_policy.plan_scan_unit_completion(
        session,
        scan_unit_id,
        assigned_server_id=request.assigned_server_id,
        attempt_count=request.attempt_count,
    )
    if not plan.should_apply:
        return plan.unit
    job = get_job_or_raise(session, plan.unit.job_id)
    manifest = manifest_ports.lock_manifest_for_scan_unit_completion(
        session,
        job.id,
    )
    unit = scheduling_policy.apply_scan_unit_completion(
        plan,
        manifest_path=request.manifest_path,
        meta_path=request.meta_path,
        file_count=request.file_count,
        total_bytes=request.total_bytes,
        finished_at=utcnow(),
    )
    manifest_ports.materialize_scan_unit_completion(
        session,
        job_id=job.id,
        manifest=manifest,
        child_paths=request.child_paths,
        shards=request.shards,
        file_count=request.file_count,
        total_bytes=request.total_bytes,
    )
    session.flush()
    freeze_manifest_if_scan_complete(
        session,
        job,
        manifest,
        limits=control_limits,
    )
    return unit

def _fail_scan_unit(
    session: Session,
    scan_unit_id: int,
    request: ScanUnitFailRequest,
) -> ScanUnit:

    plan = scheduling_policy.plan_scan_unit_failure(
        session,
        scan_unit_id,
        assigned_server_id=request.assigned_server_id,
        attempt_count=request.attempt_count,
    )
    if not plan.should_apply:
        return plan.unit
    job = get_job_or_raise(session, plan.unit.job_id)
    now = utcnow()
    unit = scheduling_policy.apply_scan_unit_failure(
        plan,
        failure_category=_scan_unit_failure_category(request),
        error_message=request.error_message,
        finished_at=now,
    )
    session.flush()
    fail_manifest_if_scan_complete(session, job.id)
    return unit

def finalize_stopped_job_if_idle(session: Session, job: Job) -> bool:
    from ...scheduling import _flush_finalization

    _flush_finalization(session)
    locked_job = scheduling_policy._lock_job_for_shard_change(session, job.id)
    if locked_job is None:
        return False
    if not locked_job.stop_requested or locked_job.status in TERMINAL_JOB_STATUSES:
        return False
    total_work = int(
        session.execute(
            select(
                func.count(WorkShard.id)
                + select(func.count(ScanUnit.id))
                .where(ScanUnit.job_id == locked_job.id)
                .scalar_subquery()
            )
            .where(WorkShard.job_id == locked_job.id)
        ).scalar_one()
        or 0
    )
    if total_work == 0:
        return False
    return scheduling_policy._finalize_job_after_shard_change(
        session,
        locked_job,
        now=utcnow(),
    )

def _claim_next_pending_shard(
    session: Session,
    job_id: str,
    server_id: str,
) -> WorkShard | None:

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
    reconciliation = scheduling_policy._reconcile_expired_shard_leases(
        session,
        now=now,
        job_id=job_id,
    )
    if reconciliation.changed:
        from ...scheduling import _flush_reconciliation

        _flush_reconciliation(session)
    job = scheduling_policy._lock_claim_parent_job(session, job_id)
    if job is None:
        return None
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
    shard_id = session.execute(
        scheduling_policy._claimable_shard_id_select(job_id)
    ).scalar_one_or_none()
    if shard_id is None:
        return None

    started_at = now
    lease_expires_at = scheduling_policy.shard_lease_deadline(now)
    return scheduling_policy._claim_work_shard(
        session,
        shard_id=shard_id,
        job_id=job_id,
        server_id=server_id,
        started_at=started_at,
        lease_expires_at=lease_expires_at,
        reclaimable_statuses=RECLAIMABLE_SHARD_STATUSES,
        non_claimable_job_statuses=non_claimable_statuses,
    )

claim_next_scan_unit_phase = _claim_next_scan_unit_phase
complete_scan_unit = _complete_scan_unit
fail_scan_unit = _fail_scan_unit
claim_next_pending_shard = _claim_next_pending_shard

__all__ = [name for name in globals() if not name.startswith("__")]
