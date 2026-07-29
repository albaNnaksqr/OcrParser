"""Job aggregate state-transition policy.

Application commands own transactions and cross-domain coordination.  This
module owns state changes on the Job aggregate itself.
"""

from __future__ import annotations

from typing import Any

from ocr_parser.infra.failure_category import infer_failure_category
from sqlalchemy import distinct, func, select, update
from sqlalchemy.orm import Session

from ...models import Job
from ...models import JobEvent, JobFile
from ...schemas import JobEventRequest
from ..common import TERMINAL_JOB_STATUSES, utcnow


def request_stop(job: Job) -> None:
    job.stop_requested = True
    if job.status == "queued":
        job.status = "stopped"
        if job.finished_at is None:
            job.finished_at = utcnow()
    elif job.status == "running":
        job.status = "stopping"


def archive(job: Job) -> None:
    if job.archived_at is None:
        job.archived_at = utcnow()


def stop_for_archived_worker(job: Job, *, now) -> None:
    job.stop_requested = True
    job.status = "stopped"
    if job.failure_category is None:
        job.failure_category = "operator_stopped"
    if job.finished_at is None:
        job.finished_at = now


def claim_queued(
    session: Session,
    job_id: str,
    *,
    started_at,
) -> bool:
    result = session.execute(
        update(Job)
        .where(Job.id == job_id)
        .where(Job.status == "queued")
        .values(status="running", started_at=started_at)
    )
    return result.rowcount == 1


def start_if_queued(job: Job, *, started_at) -> None:
    if job.status == "queued":
        job.status = "running"
        job.started_at = started_at


def apply_terminal_event(
    job: Job,
    *,
    event_type: str,
    terminal_status: str | None,
    failure_category: str | None,
    error_message: str | None,
) -> None:
    if terminal_status is None:
        return
    stop_active = job.status == "stopping" or (
        job.stop_requested and job.status not in TERMINAL_JOB_STATUSES
    )
    if stop_active:
        job.status = "stopped"
        if job.failure_category is None:
            job.failure_category = "operator_stopped"
        if job.finished_at is None:
            job.finished_at = utcnow()
    elif job.status not in TERMINAL_JOB_STATUSES:
        job.status = terminal_status
        if event_type == "job_failed":
            job.failure_category = failure_category
            job.error_message = error_message
        job.finished_at = utcnow()


def _page_no(payload: dict[str, Any]) -> int | None:
    page_no = payload.get("page_no")
    return None if page_no is None else int(page_no)


def upsert_file_from_event(
    session: Session,
    job: Job,
    event: JobEventRequest,
) -> None:
    payload = event.payload
    file_path = payload.get("file_path")
    if not file_path:
        return
    filename = payload.get("filename") or file_path.rsplit("/", 1)[-1]
    job_file = session.execute(
        select(JobFile)
        .where(JobFile.job_id == job.id)
        .where(JobFile.file_path == file_path)
    ).scalar_one_or_none()
    if job_file is None:
        job_file = JobFile(
            job_id=job.id,
            file_path=file_path,
            filename=filename,
            status="pending",
            done_pages=0,
        )
        session.add(job_file)

    if event.type == "file_started":
        job_file.status = "running"
        job_file.error = None
        job_file.failure_category = None
        if payload.get("total_pages") is not None:
            job_file.total_pages = int(payload["total_pages"])
    elif event.type == "page_done":
        page_no = _page_no(payload)
        if page_no is not None:
            done_pages_stmt = (
                select(func.count(distinct(JobEvent.page_no)))
                .where(JobEvent.job_id == job.id)
                .where(JobEvent.file_path == file_path)
                .where(JobEvent.event_type == "page_done")
                .where(JobEvent.page_no.is_not(None))
            )
            job_file.done_pages = int(
                session.execute(done_pages_stmt).scalar_one()
            )
        job_file.status = "running"
        if payload.get("status") in {"error", "failed"}:
            job_file.status = "failed"
            job_file.error = payload.get("error")
            job_file.failure_category = infer_failure_category(payload)
    elif event.type == "file_done":
        job_file.status = payload.get("status") or "success"
        job_file.output_path = payload.get("output_path")
        job_file.error = None
        job_file.failure_category = None
    elif event.type == "file_failed":
        job_file.status = "failed"
        job_file.error = payload.get("error")
        job_file.failure_category = infer_failure_category(payload)
