"""Job log mutation and read ownership."""

from __future__ import annotations

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ...limits import ControlLimits, legacy_control_limits
from ...models import JobLog
from ...schemas import (
    JobLogListResponse,
    JobLogRequest,
    JobLogResponse,
)
from .lifecycle import get_or_raise


def record(
    session: Session,
    job_id: str,
    request: JobLogRequest,
    *,
    limits: ControlLimits | None = None,
) -> JobLog:
    control_limits = limits if limits is not None else legacy_control_limits()
    log_limit = control_limits.job_log_detail_limit
    get_or_raise(session, job_id)
    if log_limit == 0:
        return JobLog(
            job_id=job_id,
            server_id=request.server_id,
            stream=request.stream,
            line=request.line,
        )
    row = JobLog(
        job_id=job_id,
        server_id=request.server_id,
        stream=request.stream,
        line=request.line,
    )
    session.add(row)
    session.flush()
    if log_limit >= 0:
        recent_log_ids = list(
            session.execute(
                select(JobLog.id)
                .where(JobLog.job_id == job_id)
                .order_by(JobLog.created_at.desc(), JobLog.id.desc())
                .limit(log_limit + 1)
            ).scalars()
        )
        stale_log_ids = recent_log_ids[log_limit:]
        if stale_log_ids:
            session.execute(delete(JobLog).where(JobLog.id.in_(stale_log_ids)))
    return row


def to_response(row: JobLog) -> JobLogResponse:
    return JobLogResponse(
        id=row.id,
        job_id=row.job_id,
        server_id=row.server_id,
        stream=row.stream,
        line=row.line,
        created_at=row.created_at,
    )


def list_page(
    session: Session,
    job_id: str,
    *,
    limit: int,
    offset: int,
    server_id: str | None = None,
    stream: str | None = None,
) -> JobLogListResponse:
    get_or_raise(session, job_id)
    filters = [JobLog.job_id == job_id]
    if server_id:
        filters.append(JobLog.server_id == server_id)
    if stream:
        filters.append(JobLog.stream == stream)
    total = int(
        session.execute(select(func.count(JobLog.id)).where(*filters)).scalar_one()
    )
    rows = list(
        session.execute(
            select(JobLog)
            .where(*filters)
            .order_by(JobLog.created_at.desc(), JobLog.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return JobLogListResponse(
        job_id=job_id,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
        items=[to_response(row) for row in rows],
    )


__all__ = ["list_page", "record", "to_response"]
