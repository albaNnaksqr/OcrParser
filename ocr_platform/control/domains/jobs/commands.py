from __future__ import annotations

from typing import Callable as __Callable
from typing import TypeVar as __TypeVar

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...limits import ControlLimits, legacy_control_limits
from ...models import Job, JobLog
from ...schemas import JobCreateRequest as __JobCreateRequest
from ...schemas import JobEventRequest, JobLogRequest
from ...settings import ControlSettings as __ControlSettings
from ..common import JobNotTerminalError, UnknownJobError
from . import events as __events
from . import lifecycle as __lifecycle
from . import logs as __logs


class JobCommandTransactionError(RuntimeError):
    """Raised when a Job command cannot own its transaction."""


RECORD_EVENT_ACTIVE_TRANSACTION_ERROR = (
    "record_event requires a session without an active transaction"
)
RECORD_LOG_ACTIVE_TRANSACTION_ERROR = (
    "record_log requires a session without an active transaction"
)
JOB_COMMAND_ACTIVE_TRANSACTION_ERROR = (
    "job command requires a session without an active transaction"
)
CommandResult = __TypeVar("CommandResult")


def _run_job_command(
    session: Session,
    operation: __Callable[[], CommandResult],
) -> CommandResult:
    if session.in_transaction():
        raise JobCommandTransactionError(JOB_COMMAND_ACTIVE_TRANSACTION_ERROR)
    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            result = operation()
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return result


def create_job(
    session: Session,
    request: __JobCreateRequest,
    *,
    settings: __ControlSettings | None = None,
    limits: ControlLimits | None = None,
) -> Job:
    return _run_job_command(
        session,
        lambda: __lifecycle.create(
            session,
            request,
            settings=settings,
            limits=limits,
        ),
    )


def request_stop(session: Session, job_id: str) -> Job:
    return _run_job_command(
        session,
        lambda: __lifecycle.request_stop(session, job_id),
    )


def archive_job(session: Session, job_id: str) -> Job:
    return _run_job_command(
        session,
        lambda: __lifecycle.archive(session, job_id),
    )


def delete_job(session: Session, job_id: str) -> None:
    _run_job_command(
        session,
        lambda: __lifecycle.delete(session, job_id),
    )


def _refresh_job_summary(session: Session, job: Job) -> None:
    from ...scheduling import (
        reconcile_expired_scan_unit_leases,
        reconcile_expired_shard_leases,
    )
    from ..manifests.core import finalize_stopped_job_if_idle

    reconcile_expired_shard_leases(session, job_id=job.id)
    reconcile_expired_scan_unit_leases(session, job_id=job.id)
    session.flush()
    finalize_stopped_job_if_idle(session, job)
    session.flush()


def refresh_job_summary(session: Session, job_id: str) -> None:
    def operation() -> None:
        job = __lifecycle.get_or_raise(session, job_id)
        _refresh_job_summary(session, job)

    _run_job_command(session, operation)


def refresh_job_summaries(
    session: Session,
    *,
    status: str | None,
    include_archived: bool,
    limit: int,
    offset: int,
) -> None:
    def operation() -> None:
        normalized_status = __lifecycle.normalize_status_filter(status)
        stmt = select(Job).order_by(Job.created_at.desc())
        if normalized_status:
            stmt = stmt.where(Job.status == normalized_status)
        if not include_archived:
            stmt = stmt.where(Job.archived_at.is_(None))
        jobs = session.execute(
            stmt.offset(max(offset, 0)).limit(max(limit, 1))
        ).scalars().all()
        for job in jobs:
            _refresh_job_summary(session, job)

    _run_job_command(session, operation)


def record_event(
    session: Session,
    job_id: str,
    event: JobEventRequest,
    *,
    limits: ControlLimits | None = None,
) -> Job:
    if session.in_transaction():
        raise JobCommandTransactionError(
            RECORD_EVENT_ACTIVE_TRANSACTION_ERROR
        )

    control_limits = limits if limits is not None else legacy_control_limits()
    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            job = __events.record_event(
                session,
                job_id,
                event,
                limits=control_limits,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return job


def record_log(
    session: Session,
    job_id: str,
    request: JobLogRequest,
    *,
    limits: ControlLimits | None = None,
) -> JobLog:
    if session.in_transaction():
        raise JobCommandTransactionError(
            RECORD_LOG_ACTIVE_TRANSACTION_ERROR
        )

    control_limits = limits if limits is not None else legacy_control_limits()
    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            row = __logs.record(
                session,
                job_id,
                request,
                limits=control_limits,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return row

__all__ = [
    "JobCommandTransactionError",
    "JobNotTerminalError",
    "JOB_COMMAND_ACTIVE_TRANSACTION_ERROR",
    "RECORD_EVENT_ACTIVE_TRANSACTION_ERROR",
    "RECORD_LOG_ACTIVE_TRANSACTION_ERROR",
    "UnknownJobError",
    "archive_job",
    "create_job",
    "delete_job",
    "record_event",
    "record_log",
    "refresh_job_summaries",
    "refresh_job_summary",
    "request_stop",
]
