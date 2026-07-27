"""Deterministic, read-only Prometheus snapshot for the Control plane."""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Mapping

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ocr_parser.contracts.observability import (
    engine_label,
    failure_category_label,
    fallback_category_label,
    stage_label,
    status_label,
)

from ...limits import ControlLimits as __ControlLimits
from ..common import JOB_STATUS_FILTERS, POOL_SERVER_ID, TERMINAL_JOB_STATUSES
from ...models import Job, JobEvent, JobFile, Server, WorkShard


PROMETHEUS_CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
TRACE_EVENT_LIMIT = 10_000
TRACE_WINDOW = timedelta(minutes=60)
EXECUTABLE_JOB_STATUSES = tuple(
    sorted(JOB_STATUS_FILTERS - TERMINAL_JOB_STATUSES - {"stopping"})
)
ALLOWED_LABEL_KEYS = frozenset(
    {
        "engine",
        "stage",
        "status",
        "failure_category",
        "fallback_category",
    }
)


@dataclass(frozen=True)
class MetricFamily:
    name: str
    help: str
    labels: tuple[str, ...] = ()


METRIC_FAMILIES = (
    MetricFamily(
        "ocr_platform_artifact_records",
        "Persisted job-file records with a declared output artifact.",
        ("engine", "status"),
    ),
    MetricFamily(
        "ocr_platform_failure_events",
        "Retained failure event records by bounded category.",
        ("engine", "failure_category"),
    ),
    MetricFamily(
        "ocr_platform_fallbacks",
        "Page fallback selections among the latest 10000 retained page_done "
        "events created in the rolling 60 minute window.",
        ("engine", "stage", "fallback_category"),
    ),
    MetricFamily(
        "ocr_platform_shard_queue",
        "Persisted claimable shard records by bounded status.",
        ("engine", "status"),
    ),
    MetricFamily(
        "ocr_platform_stage_outcomes",
        "Page stage outcomes among the latest 10000 retained page_done events "
        "created in the rolling 60 minute window.",
        ("engine", "stage", "status", "failure_category"),
    ),
    MetricFamily(
        "ocr_platform_trace_window_truncated",
        "Whether more than 10000 retained page_done events exist in the "
        "rolling 60 minute window.",
    ),
    MetricFamily(
        "ocr_platform_worker_slots",
        "Configured slots on non-archived worker records.",
    ),
    MetricFamily(
        "ocr_platform_worker_slots_by_status",
        "Configured slots on non-archived workers by bounded status.",
        ("status",),
    ),
    MetricFamily(
        "ocr_platform_workers",
        "Non-archived worker records.",
    ),
    MetricFamily(
        "ocr_platform_workers_by_status",
        "Non-archived worker records by bounded status.",
        ("status",),
    ),
)
FAMILY_BY_NAME = {family.name: family for family in METRIC_FAMILIES}

if tuple(sorted(FAMILY_BY_NAME)) != tuple(
    family.name for family in METRIC_FAMILIES
):
    raise RuntimeError("metric families must be sorted")
if any(
    not set(family.labels).issubset(ALLOWED_LABEL_KEYS)
    for family in METRIC_FAMILIES
):
    raise RuntimeError("metric family uses a forbidden label key")


def _safe_json_object(payload: object) -> dict[str, object]:
    if not isinstance(payload, str):
        return {}
    try:
        value = json.loads(payload)
    except (TypeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _nonnegative_int(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _resolve_trace_limit(
    limits: __ControlLimits | None,
) -> int:
    if limits is None:
        return TRACE_EVENT_LIMIT
    return max(0, limits.metrics_trace_event_limit)


def _add(
    samples: dict[str, Counter[tuple[str, ...]]],
    family: str,
    labels: tuple[str, ...],
    value: object,
) -> None:
    definition = FAMILY_BY_NAME[family]
    if len(labels) != len(definition.labels):
        raise ValueError("metric sample label arity mismatch")
    samples[family][labels] += _nonnegative_int(value)


def _worker_samples(
    session: Session,
    samples: dict[str, Counter[tuple[str, ...]]],
) -> None:
    rows = session.execute(
        select(
            Server.status,
            Server.capacity_slots,
        )
        .where(Server.archived_at.is_(None))
        .where(Server.id != POOL_SERVER_ID)
    ).all()
    total_workers = 0
    total_slots = 0
    for persisted_status, persisted_slots in rows:
        bounded_status = status_label(persisted_status)
        slot_count = _nonnegative_int(persisted_slots)
        total_workers += 1
        total_slots += slot_count
        _add(
            samples,
            "ocr_platform_workers_by_status",
            (bounded_status,),
            1,
        )
        _add(
            samples,
            "ocr_platform_worker_slots_by_status",
            (bounded_status,),
            slot_count,
        )
    _add(samples, "ocr_platform_workers", (), total_workers)
    _add(samples, "ocr_platform_worker_slots", (), total_slots)


def _shard_queue_samples(
    session: Session,
    samples: dict[str, Counter[tuple[str, ...]]],
) -> None:
    rows = session.execute(
        select(Job.engine, WorkShard.status, func.count(WorkShard.id))
        .join(Job, Job.id == WorkShard.job_id)
        .where(WorkShard.status.in_(("pending", "retrying", "stale")))
        .where(Job.status.in_(EXECUTABLE_JOB_STATUSES))
        .group_by(Job.engine, WorkShard.status)
    ).all()
    for engine, status, count in rows:
        _add(
            samples,
            "ocr_platform_shard_queue",
            (engine_label(engine), status_label(status)),
            count,
        )


def _failure_samples(
    session: Session,
    samples: dict[str, Counter[tuple[str, ...]]],
) -> None:
    rows = session.execute(
        select(
            Job.engine,
            JobEvent.failure_category,
            func.count(JobEvent.id),
        )
        .join(Job, Job.id == JobEvent.job_id)
        .where(JobEvent.failure_category.is_not(None))
        .group_by(Job.engine, JobEvent.failure_category)
    ).all()
    for engine, category, count in rows:
        _add(
            samples,
            "ocr_platform_failure_events",
            (engine_label(engine), failure_category_label(category)),
            count,
        )


def _trace_samples(
    session: Session,
    samples: dict[str, Counter[tuple[str, ...]]],
    *,
    now: datetime,
    trace_limit: int,
) -> None:
    cutoff = now - TRACE_WINDOW
    rows = session.execute(
        select(Job.engine, JobEvent.payload_json)
        .join(Job, Job.id == JobEvent.job_id)
        .where(JobEvent.event_type == "page_done")
        .where(JobEvent.created_at >= cutoff)
        .where(JobEvent.created_at <= now)
        .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
        .limit(trace_limit + 1)
    ).all()
    truncated = len(rows) > trace_limit
    _add(
        samples,
        "ocr_platform_trace_window_truncated",
        (),
        int(truncated),
    )
    for engine, payload_json in rows[:trace_limit]:
        payload = _safe_json_object(payload_json)
        stages = payload.get("stages")
        if isinstance(stages, list):
            for stage in stages:
                if not isinstance(stage, Mapping):
                    continue
                _add(
                    samples,
                    "ocr_platform_stage_outcomes",
                    (
                        engine_label(engine),
                        stage_label(stage.get("stage")),
                        status_label(stage.get("status")),
                        failure_category_label(
                            stage.get("failure_category")
                        ),
                    ),
                    1,
                )
        fallback = payload.get("fallback")
        if isinstance(fallback, Mapping) and fallback.get("used") is True:
            _add(
                samples,
                "ocr_platform_fallbacks",
                (
                    engine_label(engine),
                    stage_label(fallback.get("source_stage")),
                    fallback_category_label(fallback.get("reason")),
                ),
                1,
            )


def _artifact_samples(
    session: Session,
    samples: dict[str, Counter[tuple[str, ...]]],
) -> None:
    rows = session.execute(
        select(Job.engine, JobFile.status, func.count(JobFile.id))
        .join(Job, Job.id == JobFile.job_id)
        .where(JobFile.output_path.is_not(None))
        .where(JobFile.output_path != "")
        .group_by(Job.engine, JobFile.status)
    ).all()
    for engine, status, count in rows:
        _add(
            samples,
            "ocr_platform_artifact_records",
            (engine_label(engine), status_label(status)),
            count,
        )


def metrics_snapshot(
    session: Session,
    *,
    now: datetime | None = None,
    limits: __ControlLimits | None = None,
) -> dict[str, Counter[tuple[str, ...]]]:
    """Read one immutable metrics snapshot without mutating the session."""

    current_time = now or datetime.now(timezone.utc)
    if current_time.tzinfo is None:
        current_time = current_time.replace(tzinfo=timezone.utc)
    trace_limit = _resolve_trace_limit(limits)
    samples = {
        family.name: Counter()
        for family in METRIC_FAMILIES
    }
    with session.no_autoflush:
        _worker_samples(session, samples)
        _shard_queue_samples(session, samples)
        _failure_samples(session, samples)
        _trace_samples(
            session,
            samples,
            now=current_time,
            trace_limit=trace_limit,
        )
        _artifact_samples(session, samples)
    return samples


def _escape_label(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace('"', '\\"')
    )


def encode_prometheus(
    samples: Mapping[str, Counter[tuple[str, ...]]],
) -> str:
    """Encode only the statically declared Control metric families."""

    lines: list[str] = []
    for family in METRIC_FAMILIES:
        lines.append(f"# HELP {family.name} {family.help}")
        lines.append(f"# TYPE {family.name} gauge")
        family_samples = samples.get(family.name, Counter())
        for labels, value in sorted(family_samples.items()):
            label_text = ""
            if family.labels:
                encoded = ",".join(
                    f'{key}="{_escape_label(label)}"'
                    for key, label in zip(family.labels, labels)
                )
                label_text = "{" + encoded + "}"
            lines.append(
                f"{family.name}{label_text} {_nonnegative_int(value)}"
            )
    return "\n".join(lines) + "\n"


def render_control_metrics(
    session: Session,
    *,
    now: datetime | None = None,
    limits: __ControlLimits | None = None,
) -> str:
    return encode_prometheus(
        metrics_snapshot(
            session,
            now=now,
            limits=limits,
        )
    )


__all__ = [
    "ALLOWED_LABEL_KEYS",
    "FAMILY_BY_NAME",
    "METRIC_FAMILIES",
    "PROMETHEUS_CONTENT_TYPE",
    "TRACE_EVENT_LIMIT",
    "TRACE_WINDOW",
    "MetricFamily",
    "encode_prometheus",
    "metrics_snapshot",
    "render_control_metrics",
]
