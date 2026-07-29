from __future__ import annotations

from ocr_parser.infra.failure_category import infer_failure_category
from sqlalchemy.orm import Session

from ...limits import ControlLimits as __ControlLimits
from ...limits import legacy_control_limits as __legacy_control_limits
from ...models import Job, JobEvent
from ...schemas import JobEventRequest
from ..common import *
from . import policy as __policy
from .counters import parse_page_no, prune_job_detail_rows, update_job_counter_from_event, upsert_job_file_from_event
from .lifecycle import get_or_raise as get_job_or_raise

def has_static_shards(*args, **kwargs):
    from ..manifests.core import has_static_shards as target
    return target(*args, **kwargs)

def record_event(
    session: Session,
    job_id: str,
    event: JobEventRequest,
    *,
    limits: __ControlLimits | None = None,
) -> Job:
    control_limits = (
        limits if limits is not None else __legacy_control_limits()
    )
    job = get_job_or_raise(session, job_id)
    payload = event.payload
    page_no = parse_page_no(payload)
    event_time = utcnow()
    update_job_counter_from_event(
        session,
        job,
        event,
        event_time=event_time,
        limits=control_limits,
    )
    if (
        control_limits.persist_job_event_details
        or event.type in RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED
    ):
        failure_category = (
            infer_failure_category(payload)
            if event.type in PRIORITY_FAILURE_EVENT_TYPES
            else None
        )
        row = JobEvent(
            job_id=job.id,
            event_type=event.type,
            file_path=payload.get("file_path"),
            page_no=page_no,
            status=payload.get("status"),
            failure_category=failure_category,
            payload_json=json_dumps(payload),
            created_at=event_time,
        )
        session.add(row)
        session.flush()
    if control_limits.persist_job_file_details:
        upsert_job_file_from_event(session, job, event)
    session.flush()
    prune_job_detail_rows(
        session,
        job.id,
        limits=control_limits,
    )

    terminal_status = TERMINAL_EVENT_STATUSES.get(event.type)
    is_static_child_terminal = (
        terminal_status is not None
        and has_static_shards(session, job.id)
        and not payload.get("static_shards_final")
    )
    if terminal_status is not None and not is_static_child_terminal:
        __policy.apply_terminal_event(
            job,
            event_type=event.type,
            terminal_status=terminal_status,
            failure_category=(
                infer_failure_category(payload)
                if event.type == "job_failed"
                else None
            ),
            error_message=(
                payload.get("error") or payload.get("error_message")
                if event.type == "job_failed"
                else None
            ),
        )

    return job

__all__ = ["record_event"]
