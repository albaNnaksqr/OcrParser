from __future__ import annotations

from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...models import Job
from ...schemas import JobListResponse
from .core import (
    _normalized_status_filter,
    get_job_or_raise,
    get_job_summary,
    list_job_logs_page,
    list_job_summaries,
    list_job_summaries_page,
    list_recent_job_errors_page,
    list_recent_job_files,
)
from .schemas import job_to_response


def list_jobs(
    session: Session,
    *,
    status: Optional[str],
    include_archived: bool,
    limit: int,
    offset: int,
):
    normalized_status = _normalized_status_filter(status)
    stmt = select(Job).order_by(Job.created_at.desc()).offset(offset).limit(limit)
    if normalized_status:
        stmt = stmt.where(Job.status == normalized_status)
    if not include_archived:
        stmt = stmt.where(Job.archived_at.is_(None))
    return session.execute(stmt).scalars().all()


def list_jobs_page(
    session: Session,
    *,
    status: Optional[str],
    include_archived: bool,
    limit: int,
    offset: int,
) -> JobListResponse:
    normalized_status = _normalized_status_filter(status)
    count_stmt = select(func.count(Job.id))
    item_stmt = select(Job).order_by(Job.created_at.desc())
    if normalized_status:
        count_stmt = count_stmt.where(Job.status == normalized_status)
        item_stmt = item_stmt.where(Job.status == normalized_status)
    if not include_archived:
        count_stmt = count_stmt.where(Job.archived_at.is_(None))
        item_stmt = item_stmt.where(Job.archived_at.is_(None))
    total = int(session.execute(count_stmt).scalar_one() or 0)
    jobs = session.execute(item_stmt.offset(offset).limit(limit)).scalars().all()
    return JobListResponse(
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(jobs) < total,
        items=[job_to_response(job, session) for job in jobs],
    )


__all__ = [
    "get_job_or_raise",
    "get_job_summary",
    "list_job_logs_page",
    "list_job_summaries",
    "list_job_summaries_page",
    "list_jobs",
    "list_jobs_page",
    "list_recent_job_errors_page",
    "list_recent_job_files",
]
