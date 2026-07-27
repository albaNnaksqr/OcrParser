from __future__ import annotations

from sqlalchemy.orm import Session

from ...limits import ControlLimits, legacy_control_limits
from ...models import Job, JobLog
from ...schemas import JobEventRequest, JobLogRequest
from ..common import JobNotTerminalError, UnknownJobError
from . import core
from .core import archive_job, create_job, delete_job, request_stop


class JobCommandTransactionError(RuntimeError):
    """Raised when a Job command cannot own its transaction."""


RECORD_EVENT_ACTIVE_TRANSACTION_ERROR = (
    "record_event requires a session without an active transaction"
)
RECORD_LOG_ACTIVE_TRANSACTION_ERROR = (
    "record_log requires a session without an active transaction"
)


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
            job = core.record_event(
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
            row = core.record_log(
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
    "RECORD_EVENT_ACTIVE_TRANSACTION_ERROR",
    "RECORD_LOG_ACTIVE_TRANSACTION_ERROR",
    "UnknownJobError",
    "archive_job",
    "create_job",
    "delete_job",
    "record_event",
    "record_log",
    "request_stop",
]
