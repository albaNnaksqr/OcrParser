from __future__ import annotations

import os
from pathlib import Path

from collections.abc import Iterator

from .models import ManifestItem, ManifestScanResult

DEFAULT_SCAN_ERROR_SAMPLE_LIMIT = 5


def _failure_category_for_scan_error(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "input_missing"
    return "input_invalid"


def _skipped_error(path: str | Path, exc: BaseException) -> dict[str, str]:
    return {
        "path": str(path),
        "reason": str(exc),
        "failure_category": _failure_category_for_scan_error(exc),
    }


def _record_skipped_error(
    skipped_errors: list[dict[str, str]] | None,
    stats: dict[str, int] | None,
    path: str | Path,
    exc: BaseException,
    *,
    max_samples: int,
) -> None:
    if stats is not None:
        stats["skipped_error_count"] = int(stats.get("skipped_error_count", 0)) + 1
    if skipped_errors is not None and len(skipped_errors) < max_samples:
        skipped_errors.append(_skipped_error(path, exc))


def _stat_manifest_file(path: Path) -> os.stat_result:
    return path.stat()


def scan_folder_snapshot(input_root: str | Path) -> ManifestScanResult:
    root = Path(input_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"input root not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"input root is not a directory: {root}")

    items: list[ManifestItem] = []
    skipped_errors: list[dict[str, str]] = []
    scan_stats = {"skipped_error_count": 0, "scanned_dirs": 0}
    dirs = [root]
    while dirs:
        current = dirs.pop()
        scan_stats["scanned_dirs"] += 1
        try:
            with os.scandir(current) as entries:
                entry_list = list(entries)
        except OSError as exc:
            _record_skipped_error(
                skipped_errors,
                scan_stats,
                current,
                exc,
                max_samples=DEFAULT_SCAN_ERROR_SAMPLE_LIMIT,
            )
            continue

        for entry in entry_list:
            try:
                if entry.is_dir(follow_symlinks=False):
                    dirs.append(Path(entry.path))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    continue
            except OSError as exc:
                _record_skipped_error(
                    skipped_errors,
                    scan_stats,
                    entry.path,
                    exc,
                    max_samples=DEFAULT_SCAN_ERROR_SAMPLE_LIMIT,
                )
                continue
            if not entry.name.lower().endswith(".pdf"):
                continue

            raw_path = Path(entry.path)
            try:
                path = raw_path.resolve()
                stat = _stat_manifest_file(path)
                items.append(
                    ManifestItem(
                        input_path=str(path),
                        relative_path=path.relative_to(root).as_posix(),
                        size_bytes=int(stat.st_size),
                        mtime_ns=int(stat.st_mtime_ns),
                    )
                )
            except OSError as exc:
                _record_skipped_error(
                    skipped_errors,
                    scan_stats,
                    raw_path,
                    exc,
                    max_samples=DEFAULT_SCAN_ERROR_SAMPLE_LIMIT,
                )

    items.sort(key=lambda item: item.relative_path)
    skipped_errors.sort(key=lambda error: (error["path"], error["reason"]))
    return ManifestScanResult(
        input_root=str(root),
        items=items,
        skipped_errors=skipped_errors,
        skipped_error_count=scan_stats["skipped_error_count"],
        scanned_dir_count=scan_stats["scanned_dirs"],
    )


def iter_folder_snapshot_items(
    input_root: str | Path,
    *,
    skipped_errors: list[dict[str, str]] | None = None,
    stats: dict[str, int] | None = None,
    max_skipped_error_samples: int = DEFAULT_SCAN_ERROR_SAMPLE_LIMIT,
) -> Iterator[ManifestItem]:
    root = Path(input_root).resolve()
    if not root.exists():
        raise FileNotFoundError(f"input root not found: {root}")
    if not root.is_dir():
        raise NotADirectoryError(f"input root is not a directory: {root}")

    dirs = [root]
    while dirs:
        current = dirs.pop()
        if stats is not None:
            stats["scanned_dirs"] = int(stats.get("scanned_dirs", 0)) + 1
        try:
            entries = os.scandir(current)
        except OSError as exc:
            _record_skipped_error(
                skipped_errors,
                stats,
                current,
                exc,
                max_samples=max_skipped_error_samples,
            )
            continue

        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        dirs.append(Path(entry.path))
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError as exc:
                    _record_skipped_error(
                        skipped_errors,
                        stats,
                        entry.path,
                        exc,
                        max_samples=max_skipped_error_samples,
                    )
                    continue
                if not entry.name.lower().endswith(".pdf"):
                    continue

                raw_path = Path(entry.path)
                try:
                    path = raw_path.resolve()
                    stat = _stat_manifest_file(path)
                    yield ManifestItem(
                        input_path=str(path),
                        relative_path=path.relative_to(root).as_posix(),
                        size_bytes=int(stat.st_size),
                        mtime_ns=int(stat.st_mtime_ns),
                    )
                except OSError as exc:
                    _record_skipped_error(
                        skipped_errors,
                        stats,
                        raw_path,
                        exc,
                        max_samples=max_skipped_error_samples,
                    )
