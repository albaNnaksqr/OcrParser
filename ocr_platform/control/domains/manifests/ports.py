from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import scheduling
from ...models import Job, Manifest, ScanUnit, WorkShard, utcnow
from ..common import json_dumps
from . import policy


class ScanCompletionShard(Protocol):
    shard_path: str
    file_count: int


def next_manifest_shard_index(
    session: Session,
    manifest: Manifest,
    shard_count: int,
) -> int:
    stored_next = max(int(manifest.next_shard_index or 1), 1)
    if shard_count <= 0:
        return stored_next
    conflicting_index = session.execute(
        select(WorkShard.shard_index)
        .where(WorkShard.manifest_id == manifest.id)
        .where(WorkShard.shard_index >= stored_next)
        .where(WorkShard.shard_index < stored_next + shard_count)
        .limit(1)
    ).scalar_one_or_none()
    if conflicting_index is None:
        return stored_next

    existing_max = session.execute(
        select(func.max(WorkShard.shard_index)).where(
            WorkShard.manifest_id == manifest.id
        )
    ).scalar_one()
    return max(stored_next, int(existing_max or 0) + 1)


def manifest_for_scan_unit_completion_select(job_id: str):
    return (
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
        .with_for_update()
    )


def lock_manifest_for_scan_unit_completion(
    session: Session,
    job_id: str,
) -> Manifest:
    return session.execute(
        manifest_for_scan_unit_completion_select(job_id)
    ).scalar_one()


def existing_scan_unit_paths(
    session: Session,
    job_id: str,
    paths: list[str],
) -> set[str]:
    if not paths:
        return set()
    rows = session.execute(
        select(ScanUnit.path)
        .where(ScanUnit.job_id == job_id)
        .where(ScanUnit.path.in_(paths))
    ).scalars().all()
    return {str(path) for path in rows}


def materialize_scan_unit_completion(
    session: Session,
    *,
    job_id: str,
    manifest: Manifest,
    child_paths: Sequence[str],
    shards: Sequence[ScanCompletionShard],
    file_count: int,
    total_bytes: int,
) -> None:
    unique_child_paths = list(dict.fromkeys(child_paths))
    existing_child_paths = existing_scan_unit_paths(
        session,
        job_id,
        unique_child_paths,
    )
    for child_path in unique_child_paths:
        if child_path in existing_child_paths:
            continue
        session.add(
            scheduling.new_pending_scan_unit(
                job_id=job_id,
                path=child_path,
            )
        )

    next_shard_index = next_manifest_shard_index(
        session,
        manifest,
        len(shards),
    )
    for offset, shard in enumerate(shards, start=1):
        session.add(
            scheduling.new_pending_work_shard(
                job_id=job_id,
                manifest_id=manifest.id,
                shard_index=next_shard_index + offset - 1,
                shard_path=shard.shard_path,
                file_count=shard.file_count,
            )
        )
    manifest.next_shard_index = next_shard_index + len(shards)
    manifest.file_count = int(manifest.file_count or 0) + file_count
    manifest.total_bytes = int(manifest.total_bytes or 0) + total_bytes


def freeze_manifest_if_scan_complete(
    session: Session,
    job: Job,
    manifest: Manifest,
    *,
    limits: Any,
    build_report: Callable[..., dict[str, Any]],
) -> None:
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

    if policy.begin_manifest_freeze(
        manifest,
        frozen_at=utcnow(),
    ):
        report = build_report(
            session,
            job,
            manifest,
            limits=limits,
        )
        report["frozen"] = True
        report["frozen_at"] = manifest.frozen_at.isoformat()
        manifest.freeze_report_json = json_dumps(report)


def fail_manifest_if_scan_complete(
    session: Session,
    job_id: str,
) -> None:
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
