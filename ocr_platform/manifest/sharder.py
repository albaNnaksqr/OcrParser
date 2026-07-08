from __future__ import annotations

import json
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

from .models import ManifestItem, ShardSpec, WrittenManifest
from .scanner import iter_folder_snapshot_items


def write_manifest_snapshot(
    *,
    job_id: str,
    input_root: str,
    output_dir: str | Path,
    items: list[ManifestItem],
    target_files_per_shard: int,
    input_mode: str = "folder_snapshot",
    skipped_errors: list[dict[str, str]] | None = None,
    skipped_error_count: int | Callable[[], int] | None = None,
    scanned_dir_count: int | Callable[[], int] | None = None,
    estimated_total_files: int | Callable[[], int | None] | None = None,
) -> WrittenManifest:
    return write_manifest_snapshot_streaming(
        job_id=job_id,
        input_root=input_root,
        output_dir=output_dir,
        items=items,
        target_files_per_shard=target_files_per_shard,
        input_mode=input_mode,
        skipped_errors=skipped_errors,
        skipped_error_count=skipped_error_count,
        scanned_dir_count=scanned_dir_count,
        estimated_total_files=estimated_total_files if estimated_total_files is not None else len(items),
    )


def write_manifest_snapshot_streaming(
    *,
    job_id: str,
    input_root: str,
    output_dir: str | Path,
    items: Iterable[ManifestItem],
    target_files_per_shard: int,
    input_mode: str = "folder_snapshot",
    skipped_errors: list[dict[str, str]] | None = None,
    skipped_error_count: int | Callable[[], int] | None = None,
    scanned_dir_count: int | Callable[[], int] | None = None,
    progress_interval_files: int = 0,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    progress_context: Callable[[], dict[str, object]] | None = None,
    estimated_total_files: int | Callable[[], int | None] | None = None,
) -> WrittenManifest:
    if target_files_per_shard <= 0:
        raise ValueError("target_files_per_shard must be positive")

    root = Path(output_dir)
    shard_dir = root / "shards"
    shard_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = root / "manifest.jsonl"
    meta_path = root / "manifest.meta.json"

    file_count = 0
    total_bytes = 0
    shards: list[ShardSpec] = []
    shard_index = 0
    current_shard_count = 0
    current_shard_path: Path | None = None
    current_shard_handle = None
    last_progress_payload: dict[str, object] | None = None
    last_item: ManifestItem | None = None
    started_monotonic = time.monotonic()
    scan_started_at = datetime.now(timezone.utc)

    def current_skipped_error_count() -> int:
        if callable(skipped_error_count):
            return int(skipped_error_count())
        if skipped_error_count is not None:
            return int(skipped_error_count)
        return len(skipped_errors or [])

    def current_scanned_dir_count() -> int:
        if callable(scanned_dir_count):
            return int(scanned_dir_count())
        if scanned_dir_count is not None:
            return int(scanned_dir_count)
        return 0

    def current_estimated_total_files() -> int | None:
        if callable(estimated_total_files):
            value = estimated_total_files()
        else:
            value = estimated_total_files
        if value is None:
            return None
        value = int(value)
        return value if value >= 0 else None

    def estimate_remaining(
        total_files: int | None,
        elapsed_seconds: float,
    ) -> tuple[int | None, int | None]:
        if total_files is None:
            return None, None
        remaining_files = max(total_files - file_count, 0)
        if file_count <= 0 or elapsed_seconds <= 0 or remaining_files <= 0:
            return remaining_files, 0 if remaining_files == 0 else None
        files_per_second = file_count / elapsed_seconds
        if files_per_second <= 0:
            return remaining_files, None
        return remaining_files, int(remaining_files / files_per_second)

    def report_progress(
        item: ManifestItem | None,
        status: str,
        *,
        scan_finished_at: datetime | None = None,
    ) -> None:
        nonlocal last_progress_payload
        if progress_callback is None:
            return
        elapsed_seconds = max(time.monotonic() - started_monotonic, 0.0)
        total_files = current_estimated_total_files()
        remaining_files, estimated_remaining_seconds = estimate_remaining(
            total_files,
            elapsed_seconds,
        )
        payload = {
            "status": status,
            "scanned_files": file_count,
            "estimated_total_files": total_files,
            "remaining_files": remaining_files,
            "estimated_remaining_seconds": estimated_remaining_seconds,
            "total_bytes": total_bytes,
            "shard_count": len(shards) + (1 if current_shard_count else 0),
            "current_path": item.input_path if item is not None else input_root,
            "skipped_error_count": current_skipped_error_count(),
            "skipped_errors": list(skipped_errors or [])[:5],
            "scanned_dirs": current_scanned_dir_count(),
            "scan_started_at": scan_started_at.isoformat(),
            "elapsed_seconds": round(elapsed_seconds, 4),
            "files_per_second": round(file_count / elapsed_seconds, 4)
            if elapsed_seconds > 0
            else 0.0,
        }
        if scan_finished_at is not None:
            payload["scan_finished_at"] = scan_finished_at.isoformat()
        if progress_context is not None:
            payload.update(progress_context())
        if payload == last_progress_payload:
            return
        last_progress_payload = payload
        progress_callback(payload)

    with manifest_path.open("w", encoding="utf-8") as handle:
        try:
            for item in items:
                last_item = item
                line = item.to_json_line()
                handle.write(line)
                handle.write("\n")

                if current_shard_count == 0:
                    shard_index += 1
                    current_shard_path = shard_dir / f"shard-{shard_index:06d}.jsonl"
                    current_shard_handle = current_shard_path.open("w", encoding="utf-8")

                current_shard_handle.write(line)
                current_shard_handle.write("\n")
                current_shard_count += 1
                file_count += 1
                total_bytes += item.size_bytes

                if current_shard_count >= target_files_per_shard:
                    current_shard_handle.close()
                    shards.append(
                        ShardSpec(
                            index=shard_index,
                            path=current_shard_path,
                            file_count=current_shard_count,
                        )
                    )
                    current_shard_handle = None
                    current_shard_path = None
                    current_shard_count = 0
                if progress_interval_files > 0 and file_count % progress_interval_files == 0:
                    report_progress(item, "running")
        finally:
            if current_shard_handle is not None:
                current_shard_handle.close()

    if current_shard_path is not None and current_shard_count:
        shards.append(
            ShardSpec(
                index=shard_index,
                path=current_shard_path,
                file_count=current_shard_count,
            )
        )
        current_shard_count = 0
    scan_finished_at = datetime.now(timezone.utc)
    if progress_callback is not None:
        report_progress(last_item, "done", scan_finished_at=scan_finished_at)
    meta = {
        "job_id": job_id,
        "input_mode": input_mode,
        "input_root": input_root,
        "created_at": scan_finished_at.isoformat(),
        "scan_started_at": scan_started_at.isoformat(),
        "scan_finished_at": scan_finished_at.isoformat(),
        "scanner_version": "1",
        "file_count": file_count,
        "estimated_total_files": current_estimated_total_files(),
        "total_bytes": total_bytes,
        "options": {
            "recursive": True,
            "include_globs": ["**/*.pdf"],
            "exclude_globs": [],
            "follow_symlinks": False,
        },
        "skipped_error_count": current_skipped_error_count(),
        "skipped_errors": list(skipped_errors or [])[:5],
        "scanned_dir_count": current_scanned_dir_count(),
    }
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    return WrittenManifest(
        manifest_path=manifest_path,
        meta_path=meta_path,
        shards=shards,
        file_count=file_count,
        total_bytes=total_bytes,
    )


def write_folder_snapshot_streaming(
    *,
    job_id: str,
    input_root: str | Path,
    output_dir: str | Path,
    target_files_per_shard: int,
    input_mode: str = "remote_folder_snapshot",
    progress_interval_files: int = 0,
    progress_callback: Callable[[dict[str, object]], None] | None = None,
    estimated_total_files: int | Callable[[], int | None] | None = None,
) -> WrittenManifest:
    skipped_errors: list[dict[str, str]] = []
    scan_stats = {"scanned_dirs": 0, "skipped_error_count": 0}
    resolved_root = str(Path(input_root).resolve())
    return write_manifest_snapshot_streaming(
        job_id=job_id,
        input_root=resolved_root,
        output_dir=output_dir,
        items=iter_folder_snapshot_items(
            resolved_root,
            skipped_errors=skipped_errors,
            stats=scan_stats,
        ),
        target_files_per_shard=target_files_per_shard,
        input_mode=input_mode,
        skipped_errors=skipped_errors,
        skipped_error_count=lambda: scan_stats["skipped_error_count"],
        scanned_dir_count=lambda: scan_stats["scanned_dirs"],
        progress_interval_files=progress_interval_files,
        progress_callback=progress_callback,
        estimated_total_files=estimated_total_files,
        progress_context=lambda: {"scanned_dirs": scan_stats["scanned_dirs"]},
    )
