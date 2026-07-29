"""Job lifecycle ownership; transactions remain in application commands."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ... import certification_gate, database
from ...limits import ControlLimits, legacy_control_limits
from ...models import Job, Server
from ...schemas import JobCreateRequest
from ...settings import ControlSettings
from ...scheduling import (
    _lock_job_for_shard_change,
    stop_reclaimable_work_for_job,
)
from ..common import (
    ALLOWED_INPUT_MODES,
    JOB_STATUS_FILTERS,
    POOL_SERVER_ID,
    TERMINAL_JOB_STATUSES,
    JobNotTerminalError,
    UnknownJobError,
    json_dumps,
    utcnow,
)
from ..manifests.construction import (
    create_distributed_scan_for_job,
    create_static_shards_for_job,
)
from ..manifests.paths import infer_default_manifest_root
from ..manifests.use_cases import finalize_stopped_job_if_idle
from ..model_profiles.queries import effective_job_model_config
from ..workers.eligibility import candidate_workers_for_job
from ..workers.preflight import database_migration_preflight_issue
from ..workers.registration import ensure_pool_server
from . import policy


def create(
    session: Session,
    request: JobCreateRequest,
    *,
    settings: ControlSettings | None = None,
    limits: ControlLimits | None = None,
) -> Job:
    control_limits = limits if limits is not None else legacy_control_limits()
    if request.input_mode not in ALLOWED_INPUT_MODES:
        raise ValueError(f"unknown input_mode: {request.input_mode}")
    migration_issue = database_migration_preflight_issue(
        database.describe_database_status(session.get_bind())
    )
    if migration_issue is not None:
        raise ValueError(migration_issue.message)
    model_config = effective_job_model_config(
        session,
        request,
        settings=settings,
    )
    assigned_server_id = request.assigned_server_id
    if request.input_mode == "directory" and not assigned_server_id:
        raise ValueError(
            "assigned_server_id is required for directory input_mode"
        )
    if assigned_server_id and assigned_server_id != POOL_SERVER_ID:
        assigned_server = session.get(Server, assigned_server_id)
        if assigned_server is None or assigned_server.archived_at is not None:
            raise ValueError(f"unknown assigned server: {assigned_server_id}")
    allowed_server_ids = list(dict.fromkeys(request.allowed_server_ids))
    for server_id in allowed_server_ids:
        server = session.get(Server, server_id)
        if (
            server is None
            or server.archived_at is not None
            or server_id == POOL_SERVER_ID
        ):
            raise ValueError(f"unknown allowed server: {server_id}")
    certification_gate.require_job_model_profile_certification(
        session,
        request,
        candidates=candidate_workers_for_job(session, request),
    )
    if request.input_mode != "directory" and not assigned_server_id:
        assigned_server_id = ensure_pool_server(session).id
    assigned_server = (
        session.get(Server, assigned_server_id)
        if assigned_server_id is not None
        else None
    )
    if assigned_server is None or assigned_server.archived_at is not None:
        raise ValueError(f"unknown assigned server: {assigned_server_id}")
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
    session.flush()
    create_static_shards_for_job(
        session,
        job,
        request,
        limits=control_limits,
    )
    create_distributed_scan_for_job(session, job)
    return job


def normalize_status_filter(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = status.strip().lower()
    if not normalized or normalized == "all":
        return None
    if normalized not in JOB_STATUS_FILTERS:
        allowed = ", ".join(sorted(JOB_STATUS_FILTERS))
        raise ValueError(
            f"unknown job status filter: {status}; "
            f"allowed values: all, {allowed}"
        )
    return normalized


def request_stop(session: Session, job_id: str) -> Job:
    job = _lock_job_for_shard_change(session, job_id)
    if job is None:
        raise UnknownJobError(f"unknown job: {job_id}")
    policy.request_stop(job)
    stop_reclaimable_work_for_job(session, job)
    finalize_stopped_job_if_idle(session, job)
    session.flush()
    return job


def delete(session: Session, job_id: str) -> None:
    job = get_or_raise(session, job_id)
    if job.status not in TERMINAL_JOB_STATUSES:
        raise JobNotTerminalError(f"job is not terminal: {job_id}")
    session.delete(job)
    session.flush()


def archive(session: Session, job_id: str) -> Job:
    job = get_or_raise(session, job_id)
    if job.status not in TERMINAL_JOB_STATUSES:
        raise JobNotTerminalError(f"job is not terminal: {job_id}")
    if job.archived_at is None:
        policy.archive(job)
        session.flush()
    return job


def get_or_raise(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise UnknownJobError(f"unknown job: {job_id}")
    return job


def stop_assigned_queued_jobs_for_server(
    session: Session,
    server_id: str,
) -> None:
    current_time = utcnow()
    jobs = list(
        session.execute(
            select(Job)
            .where(Job.assigned_server_id == server_id)
            .where(Job.status == "queued")
        ).scalars()
    )
    for job in jobs:
        policy.stop_for_archived_worker(job, now=current_time)
        stop_reclaimable_work_for_job(session, job)
    if jobs:
        session.flush()


__all__ = [
    "archive",
    "create",
    "delete",
    "get_or_raise",
    "normalize_status_filter",
    "request_stop",
    "stop_assigned_queued_jobs_for_server",
]
