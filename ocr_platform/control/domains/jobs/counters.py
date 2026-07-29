from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ocr_parser.infra.failure_category import infer_failure_category
from sqlalchemy import case, delete, func, select
from sqlalchemy.orm import Session

from ...limits import ControlLimits as __ControlLimits
from ...limits import legacy_control_limits as __legacy_control_limits
from ...models import Job, JobCounter, JobEvent, JobFile
from ...schemas import JobEventRequest
from ..common import *
from . import policy as __policy
def parse_page_no(payload: dict[str, Any]) -> int | None:
    page_no = payload.get("page_no")
    if page_no is None:
        return None
    return int(page_no)

def upsert_job_file_from_event(session: Session, job: Job, event: JobEventRequest) -> None:
    __policy.upsert_file_from_event(session, job, event)

def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None

def get_or_create_job_counter(session: Session, job_id: str) -> JobCounter:
    counter = session.get(JobCounter, job_id)
    if counter is None:
        counter = JobCounter(job_id=job_id)
        session.add(counter)
        session.flush()
    return counter

def _load_recent_failed_file_samples(counter: JobCounter | None) -> list[dict[str, Any]]:
    if counter is None:
        return []
    try:
        value = json.loads(counter.recent_failed_files_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    samples: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("file_path") or "")
        if not file_path:
            continue
        samples.append(item)
    return samples

def _load_recent_error_samples(counter: JobCounter | None) -> list[dict[str, Any]]:
    if counter is None:
        return []
    try:
        value = json.loads(counter.recent_errors_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    samples: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if not item.get("event_type"):
            continue
        samples.append(item)
    return samples

def _load_failure_category_counts(counter: JobCounter | None) -> dict[str, int]:
    if counter is None:
        return {}
    try:
        value = json.loads(counter.failure_category_counts_json or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, count in value.items():
        try:
            numeric_count = int(count)
        except (TypeError, ValueError):
            continue
        if numeric_count > 0:
            counts[str(key)] = numeric_count
    return counts

def _increment_failure_category_count(counter: JobCounter, category: str) -> None:
    counts = _load_failure_category_counts(counter)
    counts[category] = counts.get(category, 0) + 1
    counter.failure_category_counts_json = json_dumps(
        dict(sorted(counts.items(), key=lambda item: item[0]))
    )

def _failed_file_sample_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    file_path = payload.get("file_path")
    if not file_path:
        return None
    file_path = str(file_path)
    filename = payload.get("filename") or file_path.rsplit("/", 1)[-1]
    return {
        "file_path": file_path,
        "filename": str(filename),
        "status": "failed",
        "total_pages": _optional_int(payload.get("total_pages")),
        "done_pages": _optional_int(payload.get("done_pages")) or 0,
        "output_path": payload.get("output_path"),
        "error": payload.get("error"),
        "failure_category": infer_failure_category(payload),
    }

def _store_recent_failed_file_sample(
    counter: JobCounter,
    payload: dict[str, Any],
    *,
    limits: __ControlLimits,
) -> None:
    limit = max(0, limits.job_failed_file_sample_limit)
    sample = _failed_file_sample_from_payload(payload)
    if sample is None:
        return
    if limit == 0:
        counter.recent_failed_files_json = "[]"
        return

    file_path = sample["file_path"]
    samples = [
        item
        for item in _load_recent_failed_file_samples(counter)
        if str(item.get("file_path") or "") != file_path
    ]
    samples.insert(0, sample)
    counter.recent_failed_files_json = json_dumps(samples[:limit])

def _failure_event_sample_from_event(
    event: JobEventRequest,
    *,
    event_time: datetime,
) -> dict[str, Any] | None:
    if event.type not in PRIORITY_FAILURE_EVENT_TYPES or event.type == "file_failed":
        return None
    payload = event.payload
    return {
        "event_type": event.type,
        "file_path": payload.get("file_path"),
        "filename": payload.get("filename"),
        "failure_category": infer_failure_category(payload),
        "error": payload.get("error") or payload.get("error_message"),
        "created_at": event_time.isoformat(),
        "payload": payload,
    }

def _store_recent_error_sample(
    counter: JobCounter,
    event: JobEventRequest,
    *,
    event_time: datetime,
    limits: __ControlLimits,
) -> None:
    limit = max(0, limits.job_recent_error_sample_limit)
    sample = _failure_event_sample_from_event(event, event_time=event_time)
    if sample is None:
        return
    if limit == 0:
        counter.recent_errors_json = "[]"
        return

    samples = _load_recent_error_samples(counter)
    samples.insert(0, sample)
    counter.recent_errors_json = json_dumps(samples[:limit])

def job_counter_event_already_seen(session: Session, job_id: str, event: JobEventRequest) -> bool:
    payload = event.payload
    file_path = payload.get("file_path")
    if not file_path:
        return False
    stmt = (
        select(JobEvent.id)
        .where(JobEvent.job_id == job_id)
        .where(JobEvent.event_type == event.type)
        .where(JobEvent.file_path == file_path)
    )
    if event.type == "page_done":
        page_no = parse_page_no(payload)
        if page_no is None:
            return False
        stmt = stmt.where(JobEvent.page_no == page_no)
    elif event.type not in {"file_started", "file_done", "file_failed"}:
        return False
    return session.execute(stmt.limit(1)).scalar_one_or_none() is not None

def update_job_counter_from_event(
    session: Session,
    job: Job,
    event: JobEventRequest,
    *,
    event_time: datetime,
    limits: __ControlLimits | None = None,
) -> JobCounter:
    control_limits = (
        limits if limits is not None else __legacy_control_limits()
    )
    counter = get_or_create_job_counter(session, job.id)
    if counter.first_event_at is None:
        counter.first_event_at = event_time
    counter.last_event_at = event_time

    payload = event.payload
    if job_counter_event_already_seen(session, job.id, event):
        return counter
    if event.type == "file_started":
        counter.started_files += 1
        if payload.get("total_pages") is not None:
            try:
                counter.total_pages += int(payload["total_pages"])
            except (TypeError, ValueError):
                pass
    elif event.type == "page_done":
        counter.completed_pages += 1
        if payload.get("status") in DEGRADED_PAGE_STATUSES:
            counter.degraded_pages += 1
    elif event.type == "file_done":
        if payload.get("status") == "skipped":
            counter.skipped_files += 1
        else:
            counter.completed_files += 1
    elif event.type == "file_failed":
        counter.failed_files += 1
        _increment_failure_category_count(counter, infer_failure_category(payload))
        _store_recent_failed_file_sample(
            counter,
            payload,
            limits=control_limits,
        )
    if event.type in PRIORITY_FAILURE_EVENT_TYPES:
        _store_recent_error_sample(
            counter,
            event,
            event_time=event_time,
            limits=control_limits,
        )
    return counter

def _job_counter_total_files(counter: JobCounter | None) -> int:
    if counter is None:
        return 0
    terminal_files = counter.completed_files + counter.failed_files + counter.skipped_files
    return max(counter.started_files, terminal_files)

def prune_job_detail_rows(
    session: Session,
    job_id: str,
    *,
    limits: __ControlLimits | None = None,
) -> None:
    control_limits = (
        limits if limits is not None else __legacy_control_limits()
    )
    file_limit = control_limits.job_file_detail_limit
    event_limit = control_limits.job_event_detail_limit
    retained_event_limit = (
        control_limits.retained_control_event_limit_when_details_disabled
    )
    if file_limit >= 0:
        if file_limit == 0:
            stale_file_ids = list(
                session.execute(
                    select(JobFile.id).where(JobFile.job_id == job_id)
                ).scalars()
            )
        else:
            recent_file_ids = list(
                session.execute(
                    select(JobFile.id)
                    .where(JobFile.job_id == job_id)
                    .order_by(
                        case((JobFile.status.in_(FAILED_FILE_STATUSES), 0), else_=1),
                        JobFile.updated_at.desc(),
                        JobFile.id.desc(),
                    )
                    .limit(file_limit + 1)
                ).scalars()
            )
            stale_file_ids = recent_file_ids[file_limit:]
        if stale_file_ids:
            session.execute(delete(JobFile).where(JobFile.id.in_(stale_file_ids)))
    if event_limit >= 0:
        if event_limit == 0:
            stale_event_ids = list(
                session.execute(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id)
                    .where(
                        JobEvent.event_type.not_in(
                            RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED
                        )
                    )
                ).scalars()
            )
        else:
            recent_event_ids = list(
                session.execute(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id)
                    .order_by(
                        case((JobEvent.event_type.in_(PRIORITY_FAILURE_EVENT_TYPES), 0), else_=1),
                        case((JobEvent.event_type.in_(PRIORITY_TERMINAL_EVENT_TYPES), 0), else_=1),
                        JobEvent.created_at.desc(),
                        JobEvent.id.desc(),
                    )
                    .limit(event_limit + 1)
                ).scalars()
            )
            stale_event_ids = recent_event_ids[event_limit:]
        if stale_event_ids:
            session.execute(delete(JobEvent).where(JobEvent.id.in_(stale_event_ids)))
    if event_limit == 0 and retained_event_limit >= 0:
        for event_type in RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED:
            retained_event_ids = list(
                session.execute(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id)
                    .where(JobEvent.event_type == event_type)
                    .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
                    .limit(retained_event_limit + 1)
                ).scalars()
            )
            stale_retained_event_ids = retained_event_ids[
                retained_event_limit:
            ]
            if stale_retained_event_ids:
                session.execute(
                    delete(JobEvent).where(JobEvent.id.in_(stale_retained_event_ids))
                )

__all__ = [name for name in globals() if not name.startswith("__")]
