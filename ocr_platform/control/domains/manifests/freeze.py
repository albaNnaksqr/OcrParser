"""Manifest freeze policy execution and read projection."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...limits import ControlLimits, legacy_control_limits
from ...models import Job, Manifest, ScanUnit, WorkShard
from ...schemas import ManifestFreezeReportResponse
from ..common import json_loads_object, utcnow
from . import policy
from .integrity import (
    bounded_manifest_integrity_issues,
    get_manifest_integrity_report,
    manifest_integrity_freeze_summary as _manifest_integrity_freeze_summary,
)
from .projection import (
    get_job_or_raise,
    latest_manifest_scan_progress as _latest_manifest_scan_progress,
    manifest_scan_error_samples as _manifest_scan_error_samples,
    manifest_scan_metadata as _manifest_scan_metadata,
    recent_manifest_scan_error_samples as _recent_manifest_scan_error_samples,
    scan_unit_problem_samples as _scan_unit_problem_samples,
)
def _scan_unit_status_counts(session: Session, job_id: str) -> dict[str, int]:
    rows = session.execute(
        select(ScanUnit.status, func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job_id)
        .group_by(ScanUnit.status)
    ).all()
    return {status: int(count) for status, count in rows}

def _shard_status_counts(session: Session, job_id: str) -> dict[str, int]:
    rows = session.execute(
        select(WorkShard.status, func.count(WorkShard.id))
        .where(WorkShard.job_id == job_id)
        .group_by(WorkShard.status)
    ).all()
    return {status: int(count) for status, count in rows}

def _build_manifest_freeze_report(
    session: Session,
    job: Job,
    manifest: Manifest,
    *,
    limits: ControlLimits | None = None,
) -> dict[str, Any]:
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    scan_unit_counts = _scan_unit_status_counts(session, job.id)
    shard_counts = _shard_status_counts(session, job.id)
    scan_progress = _latest_manifest_scan_progress(session, job.id)
    progress_scan_error_count = int(scan_progress.get("skipped_error_count") or 0)
    progress_scan_error_samples = _recent_manifest_scan_error_samples(session, job.id, limit=10)
    manifest_scan_meta = _manifest_scan_metadata(manifest)
    try:
        manifest_scan_error_count = int(manifest_scan_meta.get("skipped_error_count") or 0)
    except (TypeError, ValueError):
        manifest_scan_error_count = 0
    manifest_scan_error_samples = _manifest_scan_error_samples(manifest_scan_meta, limit=10)
    scan_unit_problem_samples = _scan_unit_problem_samples(session, job.id, limit=10)
    total_scan_units = sum(scan_unit_counts.values())
    total_shards = sum(shard_counts.values())
    shard_file_count = int(
        session.execute(
            select(func.coalesce(func.sum(WorkShard.file_count), 0)).where(
                WorkShard.job_id == job.id
            )
        ).scalar_one()
        or 0
    )
    manifest_file_count = int(manifest.file_count or 0)
    scan_error_samples = (
        progress_scan_error_samples
        or manifest_scan_error_samples
        or scan_unit_problem_samples
    )
    scan_error_count = max(
        progress_scan_error_count,
        manifest_scan_error_count,
        scan_unit_counts.get("failed", 0),
        scan_unit_counts.get("stale", 0),
        len(scan_error_samples),
    )
    integrity_summary = _manifest_integrity_freeze_summary(
        get_manifest_integrity_report(
            session,
            job.id,
            limits=control_limits,
        ),
        limits=control_limits,
    )
    return {
        "frozen": manifest.frozen_at is not None,
        "job_id": job.id,
        "manifest_id": manifest.id,
        "input_mode": manifest.input_mode,
        "input_root": manifest.input_root,
        "manifest_path": manifest.manifest_path,
        "meta_path": manifest.meta_path,
        "file_count": manifest_file_count,
        "total_bytes": int(manifest.total_bytes or 0),
        "shard_count": total_shards,
        "shard_file_count": shard_file_count,
        "shard_file_count_matches_manifest": shard_file_count == manifest_file_count,
        "scan_unit_count": total_scan_units,
        "scan_units": {
            "pending": scan_unit_counts.get("pending", 0),
            "running": scan_unit_counts.get("running", 0),
            "stale": scan_unit_counts.get("stale", 0),
            "succeeded": scan_unit_counts.get("succeeded", 0),
            "failed": scan_unit_counts.get("failed", 0),
        },
        "shards": {
            "pending": shard_counts.get("pending", 0),
            "running": shard_counts.get("running", 0),
            "retrying": shard_counts.get("retrying", 0),
            "stale": shard_counts.get("stale", 0),
            "succeeded": shard_counts.get("succeeded", 0),
            "failed": shard_counts.get("failed", 0),
            "stopped": shard_counts.get("stopped", 0),
        },
        "scan_error_count": scan_error_count,
        "scan_error_samples": scan_error_samples,
        "created_at": utcnow().isoformat(),
        **integrity_summary,
    }

def get_manifest_freeze_report(
    session: Session,
    job_id: str,
    *,
    limits: ControlLimits | None = None,
) -> ManifestFreezeReportResponse:
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    job = get_job_or_raise(session, job_id)
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if manifest is None:
        return ManifestFreezeReportResponse(
            job_id=job_id,
            manifest_id=None,
            status="missing_manifest",
            frozen_at=None,
            report={"frozen": False},
        )
    if manifest.frozen_at is not None:
        report = dict(
            json_loads_object(manifest.freeze_report_json)
        )
        if "integrity_issue_samples" in report:
            report["integrity_issue_samples"] = (
                bounded_manifest_integrity_issues(
                    report.get("integrity_issue_samples"),
                    limit=min(
                        max(
                            control_limits.manifest_integrity_issue_sample_limit,
                            0,
                        ),
                        5,
                    ),
                )
                if isinstance(
                    report.get("integrity_issue_samples"),
                    list,
                )
                else []
            )
        return ManifestFreezeReportResponse(
            job_id=job_id,
            manifest_id=manifest.id,
            status=manifest.status,
            frozen_at=manifest.frozen_at,
            report=report,
        )
    return ManifestFreezeReportResponse(
        job_id=job_id,
        manifest_id=manifest.id,
        status=manifest.status,
        frozen_at=None,
        report=_build_manifest_freeze_report(
            session,
            job,
            manifest,
            limits=control_limits,
        )
        | {"frozen": False},
    )
def freeze_manifest_if_scan_complete(
    session: Session,
    job: Job,
    manifest: Manifest,
    *,
    limits: ControlLimits | None = None,
    build_report: Callable[..., dict[str, Any]] | None = None,
) -> None:
    control_limits = limits if limits is not None else legacy_control_limits()
    active_units = session.execute(
        select(func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status.in_({"pending", "running", "stale"}))
    ).scalar_one()
    failed_units = session.execute(
        select(func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status == "failed")
    ).scalar_one()
    if int(active_units or 0) != 0 or int(failed_units or 0) != 0:
        return
    if policy.begin_manifest_freeze(manifest, frozen_at=utcnow()):
        report_builder = build_report or _build_manifest_freeze_report
        report = report_builder(
            session,
            job,
            manifest,
            limits=control_limits,
        )
        report["frozen"] = True
        report["frozen_at"] = manifest.frozen_at.isoformat()
        policy.store_freeze_report(manifest, report)


def fail_manifest_if_scan_complete(session: Session, job_id: str) -> None:
    active_units = session.execute(
        select(func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job_id)
        .where(ScanUnit.status.in_({"pending", "running", "stale"}))
    ).scalar_one()
    if int(active_units or 0) != 0:
        return
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if manifest is not None:
        policy.mark_manifest_failed(manifest)


build_manifest_freeze_report = _build_manifest_freeze_report

__all__ = [name for name in globals() if not name.startswith("__")]
