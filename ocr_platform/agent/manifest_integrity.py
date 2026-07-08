from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ocr_platform.manifest.models import ManifestItem


MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT = 20


class InvalidManifestRowError(ValueError):
    pass


class DuplicateManifestRelativePathError(ValueError):
    pass


class InvalidManifestRelativePathError(ValueError):
    pass


def _validate_manifest_relative_path_shape(relative_path_value: str, line_number: int) -> str:
    if "\\" in relative_path_value:
        raise InvalidManifestRelativePathError(
            f"relative_path must use POSIX '/' separators at line {line_number}"
        )
    relative_path = Path(relative_path_value)
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise InvalidManifestRelativePathError(
            f"relative_path must be relative and may not contain '..' at line {line_number}"
        )
    if not relative_path.name or relative_path.suffix.lower() != ".pdf":
        raise InvalidManifestRelativePathError(
            f"relative_path must point to a PDF file at line {line_number}"
        )
    return relative_path.as_posix()


def _count_jsonl_rows_with_relative_paths(path: Path) -> tuple[int, set[str], int]:
    count = 0
    total_bytes = 0
    seen_relative_paths: set[str] = set()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                item = ManifestItem.from_json_line(stripped)
            except json.JSONDecodeError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidManifestRowError(
                    f"invalid manifest row at line {line_number}"
                ) from exc
            relative_key = _validate_manifest_relative_path_shape(
                item.relative_path,
                line_number,
            )
            if relative_key in seen_relative_paths:
                raise DuplicateManifestRelativePathError(
                    f"duplicate relative_path at line {line_number}: {relative_key}"
                )
            seen_relative_paths.add(relative_key)
            total_bytes += item.size_bytes
            count += 1
    return count, seen_relative_paths, total_bytes


def _jsonl_error_reason(exc: BaseException) -> str:
    if isinstance(exc, OSError):
        return "file_unreadable"
    if isinstance(exc, json.JSONDecodeError):
        return "malformed_jsonl"
    if isinstance(exc, InvalidManifestRowError):
        return "invalid_manifest_row"
    if isinstance(exc, InvalidManifestRelativePathError):
        return "invalid_relative_path"
    if isinstance(exc, DuplicateManifestRelativePathError):
        return "duplicate_relative_path"
    return "file_unreadable"


def _append_issue_sample(samples: list[dict[str, Any]], issue: dict[str, Any]) -> None:
    if len(samples) < MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT:
        samples.append(issue)


def _read_meta_counts(meta_path: str | None) -> tuple[bool | None, int | None, int | None, str | None]:
    if not meta_path:
        return None, None, None, None
    path = Path(meta_path)
    if not path.exists():
        return False, None, None, None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return True, None, None, "file_unreadable"
    except json.JSONDecodeError:
        return True, None, None, "malformed_json"
    if not isinstance(payload, dict):
        return True, None, None, "malformed_json"
    actual_file_count: int | None = None
    actual_total_bytes: int | None = None
    if payload.get("file_count") is None:
        return True, None, None, "file_count_missing"
    try:
        actual_file_count = int(payload["file_count"])
    except (TypeError, ValueError):
        return True, None, None, "file_count_invalid"
    if payload.get("total_bytes") is not None:
        try:
            actual_total_bytes = int(payload["total_bytes"])
        except (TypeError, ValueError):
            return True, actual_file_count, None, "total_bytes_invalid"
    return True, actual_file_count, actual_total_bytes, None


def build_worker_manifest_integrity_report(task: dict[str, Any]) -> dict[str, Any]:
    job_id = str(task["job_id"])
    manifest_id = int(task["manifest_id"])
    manifest_path = str(task["manifest_path"])
    meta_path = task.get("meta_path")
    manifest_expected_file_count = int(task.get("manifest_expected_file_count") or 0)
    manifest_expected_total_bytes = int(task.get("manifest_expected_total_bytes") or 0)
    shards = task.get("shards") if isinstance(task.get("shards"), list) else []

    manifest_file_exists = Path(manifest_path).exists()
    manifest_actual_file_count: int | None = None
    manifest_actual_total_bytes: int | None = None
    manifest_relative_paths: set[str] | None = None
    manifest_error: str | None = None
    if manifest_file_exists:
        try:
            (
                manifest_actual_file_count,
                manifest_relative_paths,
                manifest_actual_total_bytes,
            ) = _count_jsonl_rows_with_relative_paths(Path(manifest_path))
        except (
            OSError,
            json.JSONDecodeError,
            InvalidManifestRowError,
            InvalidManifestRelativePathError,
            DuplicateManifestRelativePathError,
        ) as exc:
            manifest_error = _jsonl_error_reason(exc)
    manifest_file_count_matches = manifest_actual_file_count == manifest_expected_file_count
    manifest_total_bytes_matches = manifest_actual_total_bytes == manifest_expected_total_bytes
    if (
        manifest_error is None
        and manifest_file_count_matches
        and not manifest_total_bytes_matches
    ):
        manifest_error = "total_bytes_mismatch"

    (
        meta_file_exists,
        meta_actual_file_count,
        meta_actual_total_bytes,
        meta_error,
    ) = _read_meta_counts(str(meta_path) if meta_path else None)
    meta_file_count_matches = (
        meta_path is None
        or meta_actual_file_count == manifest_expected_file_count
    )
    meta_total_bytes_matches = (
        meta_path is None
        or meta_actual_total_bytes is None
        or meta_actual_total_bytes == manifest_expected_total_bytes
    )
    if meta_error is None and meta_path is not None and not meta_total_bytes_matches:
        meta_error = "total_bytes_mismatch"

    bad_shards: list[dict[str, Any]] = []
    bad_shard_count = 0
    shard_expected_file_count = 0
    shard_relative_paths: set[str] = set()
    for shard in shards:
        if not isinstance(shard, dict):
            continue
        shard_id = int(shard["shard_id"])
        shard_index = int(shard["shard_index"])
        shard_path = str(shard["shard_path"])
        expected_file_count = int(shard.get("expected_file_count") or 0)
        shard_expected_file_count += expected_file_count
        if not Path(shard_path).exists():
            bad_shard_count += 1
            _append_issue_sample(
                bad_shards,
                {
                    "shard_id": shard_id,
                    "shard_index": shard_index,
                    "shard_path": shard_path,
                    "expected_file_count": expected_file_count,
                    "actual_file_count": None,
                    "reason": "file_missing",
                },
            )
            continue
        try:
            actual_file_count, shard_file_relative_paths, _ = _count_jsonl_rows_with_relative_paths(
                Path(shard_path)
            )
        except (
            OSError,
            json.JSONDecodeError,
            InvalidManifestRowError,
            InvalidManifestRelativePathError,
            DuplicateManifestRelativePathError,
        ) as exc:
            bad_shard_count += 1
            _append_issue_sample(
                bad_shards,
                {
                    "shard_id": shard_id,
                    "shard_index": shard_index,
                    "shard_path": shard_path,
                    "expected_file_count": expected_file_count,
                    "actual_file_count": None,
                    "reason": _jsonl_error_reason(exc),
                },
            )
            continue
        reason: str | None = None
        actual_for_issue: int | None = actual_file_count
        if shard_relative_paths.intersection(shard_file_relative_paths):
            reason = "duplicate_relative_path"
            actual_for_issue = None
        elif manifest_relative_paths is not None and shard_file_relative_paths.difference(
            manifest_relative_paths
        ):
            reason = "relative_path_not_in_manifest"
            actual_for_issue = None
        elif actual_file_count != expected_file_count:
            reason = "file_count_mismatch"
        # Always accumulate paths regardless of shard validity so that later
        # shards are checked against all previously seen paths — including those
        # from bad shards — preventing duplicate-path misses due to shard order.
        shard_relative_paths.update(shard_file_relative_paths)
        if reason:
            bad_shard_count += 1
            _append_issue_sample(
                bad_shards,
                {
                    "shard_id": shard_id,
                    "shard_index": shard_index,
                    "shard_path": shard_path,
                    "expected_file_count": expected_file_count,
                    "actual_file_count": actual_for_issue,
                    "reason": reason,
                },
            )
            continue

    shard_file_count_matches_manifest = shard_expected_file_count == manifest_expected_file_count
    ok = (
        manifest_file_exists
        and manifest_error is None
        and manifest_file_count_matches
        and manifest_total_bytes_matches
        and meta_file_exists is not False
        and meta_error is None
        and meta_file_count_matches
        and meta_total_bytes_matches
        and shard_file_count_matches_manifest
        and bad_shard_count == 0
    )
    return {
        "job_id": job_id,
        "manifest_id": manifest_id,
        "ok": ok,
        "status": "ok" if ok else "failed",
        "manifest_path": manifest_path,
        "manifest_file_exists": manifest_file_exists,
        "manifest_expected_file_count": manifest_expected_file_count,
        "manifest_actual_file_count": manifest_actual_file_count,
        "manifest_file_count_matches": manifest_file_count_matches,
        "manifest_expected_total_bytes": manifest_expected_total_bytes,
        "manifest_actual_total_bytes": manifest_actual_total_bytes,
        "manifest_total_bytes_matches": manifest_total_bytes_matches,
        "manifest_error": manifest_error,
        "meta_path": str(meta_path) if meta_path else None,
        "meta_file_exists": meta_file_exists,
        "meta_error": meta_error,
        "meta_expected_file_count": manifest_expected_file_count,
        "meta_actual_file_count": meta_actual_file_count,
        "meta_file_count_matches": meta_file_count_matches,
        "meta_expected_total_bytes": manifest_expected_total_bytes,
        "meta_actual_total_bytes": meta_actual_total_bytes,
        "meta_total_bytes_matches": meta_total_bytes_matches,
        "scan_unit_count": 0,
        "scan_unit_manifest_expected_file_count": 0,
        "scan_unit_manifest_actual_file_count": None,
        "scan_unit_manifest_count_matches": False,
        "scan_unit_manifest_expected_total_bytes": 0,
        "scan_unit_manifest_actual_total_bytes": None,
        "scan_unit_manifest_total_bytes_matches": False,
        "bad_scan_unit_count": 0,
        "bad_scan_units": [],
        "shard_count": len(shards),
        "shard_expected_file_count": shard_expected_file_count,
        "shard_reference_file_count": manifest_expected_file_count,
        "shard_file_count_matches_manifest": shard_file_count_matches_manifest,
        "bad_shard_count": bad_shard_count,
        "bad_shards": bad_shards,
    }
