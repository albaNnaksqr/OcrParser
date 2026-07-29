from __future__ import annotations

import json
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, Protocol

from ocr_platform.manifest.models import ManifestItem
from ocr_platform.manifest.scanner import scan_folder_snapshot
from ocr_platform.manifest.sharder import write_manifest_snapshot
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ... import scheduling
from ...limits import ControlLimits
from ...limits import legacy_control_limits
from ...models import Job, Manifest, ScanUnit, WorkShard, utcnow
from ...schemas import JobCreateRequest, RemoteManifestRegisterRequest
from ..common import (
    ALLOWED_INPUT_MODES,
    CONTROL_STATIC_INPUT_MODES,
    REMOTE_DISTRIBUTED_SCAN_INPUT_MODES,
    REMOTE_STATIC_INPUT_MODES,
    UnknownJobError,
)
from . import policy
from .paths import manifest_output_dir, manifest_output_dir_for_job


def has_static_shards(session: Session, job_id: str) -> bool:
    from .projection import has_static_shards as target

    return target(session, job_id)


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
    policy.add_materialized_content(
        manifest,
        next_shard_index=next_shard_index + len(shards),
        file_count=file_count,
        total_bytes=total_bytes,
    )


def freeze_manifest_if_scan_complete(
    session: Session,
    job: Job,
    manifest: Manifest,
    *,
    limits: Any,
    build_report: Callable[..., dict[str, Any]] | None = None,
) -> None:
    from .freeze import freeze_manifest_if_scan_complete as target

    target(
        session,
        job,
        manifest,
        limits=limits,
        build_report=build_report,
    )


def fail_manifest_if_scan_complete(
    session: Session,
    job_id: str,
) -> None:
    from .freeze import fail_manifest_if_scan_complete as target

    target(session, job_id)

def _read_manifest_items(manifest_path: Path) -> list[ManifestItem]:
    items: list[ManifestItem] = []
    seen_relative_paths: set[str] = set()
    for line_number, line in enumerate(manifest_path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            item = ManifestItem.from_json_line(line)
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            raise ValueError(
                f"malformed manifest row in {manifest_path} at line {line_number}: {exc}"
            ) from exc
        relative_path = Path(item.relative_path)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError(
                f"malformed manifest row in {manifest_path} at line {line_number}: "
                f"relative_path must be relative and may not contain '..': {item.relative_path}"
            )
        relative_key = relative_path.as_posix()
        if relative_key in seen_relative_paths:
            raise ValueError(
                f"malformed manifest row in {manifest_path}: duplicate relative_path "
                f"would overwrite output: {relative_key} line {line_number}"
            )
        seen_relative_paths.add(relative_key)
        items.append(item)
    return items

def _create_static_shards_for_job(
    session: Session,
    job: Job,
    request: JobCreateRequest,
    *,
    limits: ControlLimits | None = None,
) -> None:
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    if request.input_mode not in ALLOWED_INPUT_MODES:
        raise ValueError(f"unknown input_mode: {request.input_mode}")
    if request.input_mode not in CONTROL_STATIC_INPUT_MODES:
        return

    if request.input_mode == "folder_snapshot":
        scan = scan_folder_snapshot(request.input_dir)
        items = scan.items
        input_root = scan.input_root
        skipped_errors = scan.skipped_errors
        skipped_error_count = scan.scan_error_count
        scanned_dir_count = scan.scanned_dir_count
    else:
        if not request.manifest_path:
            raise ValueError("manifest_path is required for existing_manifest input_mode")
        manifest_file = Path(request.manifest_path)
        if not manifest_file.exists():
            raise FileNotFoundError(f"manifest file not found: {manifest_file}")
        items = _read_manifest_items(manifest_file)
        input_root = request.input_dir
        skipped_errors = None
        skipped_error_count = None
        scanned_dir_count = None

    written = write_manifest_snapshot(
        job_id=job.id,
        input_root=input_root,
        output_dir=manifest_output_dir(job, request),
        items=items,
        target_files_per_shard=request.target_files_per_shard,
        input_mode=request.input_mode,
        skipped_errors=skipped_errors,
        skipped_error_count=skipped_error_count,
        scanned_dir_count=scanned_dir_count,
    )
    manifest = Manifest(
        job_id=job.id,
        input_mode=request.input_mode,
        input_root=input_root,
        manifest_path=str(written.manifest_path),
        meta_path=str(written.meta_path),
        file_count=written.file_count,
        total_bytes=written.total_bytes,
        next_shard_index=len(written.shards) + 1,
        status="ready",
    )
    session.add(manifest)
    session.flush()

    for shard in written.shards:
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=shard.index,
                shard_path=str(shard.path),
                status="pending",
                file_count=shard.file_count,
            )
        )
    session.flush()
    freeze_manifest_if_scan_complete(
        session,
        job,
        manifest,
        limits=control_limits,
    )

def _create_distributed_scan_for_job(session: Session, job: Job) -> None:
    if job.input_mode not in REMOTE_DISTRIBUTED_SCAN_INPUT_MODES:
        return
    root = manifest_output_dir_for_job(job)
    session.add(
        Manifest(
            job_id=job.id,
            input_mode=job.input_mode,
            input_root=job.input_dir,
            manifest_path=str(root / "manifest.jsonl"),
            meta_path=str(root / "manifest.meta.json"),
            file_count=0,
            total_bytes=0,
            status="scanning",
        )
    )
    session.add(ScanUnit(job_id=job.id, path=job.input_dir, status="pending"))

def register_remote_manifest(
    session: Session,
    job_id: str,
    request: RemoteManifestRegisterRequest,
) -> Manifest:
    # Lock the Job row first so concurrent registrations for the same job are
    # serialised — the two guard SELECTs below are otherwise a TOCTOU race.
    job = session.execute(select(Job).where(Job.id == job_id).with_for_update()).scalar_one_or_none()
    if job is None:
        raise UnknownJobError(f"unknown job: {job_id}")
    if job.input_mode not in REMOTE_STATIC_INPUT_MODES:
        raise ValueError(f"job input_mode does not accept remote manifest registration: {job.input_mode}")
    if has_static_shards(session, job.id):
        raise ValueError(f"job already has registered shards: {job.id}")
    if session.execute(select(func.count(Manifest.id)).where(Manifest.job_id == job.id)).scalar_one():
        raise ValueError(f"job already has registered manifest: {job.id}")
    manifest_file = Path(request.manifest_path)
    if manifest_file.exists():
        _read_manifest_items(manifest_file)

    manifest = Manifest(
        job_id=job.id,
        input_mode=request.input_mode,
        input_root=request.input_root,
        manifest_path=request.manifest_path,
        meta_path=request.meta_path,
        file_count=request.file_count,
        total_bytes=request.total_bytes,
        next_shard_index=len(request.shards) + 1,
        status="ready",
    )
    session.add(manifest)
    session.flush()
    for shard in request.shards:
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=shard.shard_index,
                shard_path=shard.shard_path,
                status="pending",
                file_count=shard.file_count,
            )
        )
    # Transaction ownership belongs to the manifest command wrapper.
    # Keep this leaf neutral so failures roll back the full registration.
    return manifest

create_static_shards_for_job = _create_static_shards_for_job
create_distributed_scan_for_job = _create_distributed_scan_for_job
read_manifest_items = _read_manifest_items
