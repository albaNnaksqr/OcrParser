"""Manifest integrity validation, worker evidence, and read projections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ocr_platform.manifest.models import ManifestItem
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...limits import ControlLimits, legacy_control_limits
from ...models import Job, Manifest, ScanUnit, Server, WorkShard
from ...schemas import (
    ManifestIntegrityResponse,
    ManifestIntegrityScanUnitIssue,
    ManifestIntegrityShardIssue,
    ManifestIntegrityWorkerCompleteRequest,
    ManifestIntegrityWorkerRequestResponse,
    ManifestIntegrityWorkerShardTask,
    ManifestIntegrityWorkerTask,
)
from ..common import POOL_SERVER_ID, json_loads_object, utcnow
from . import policy
from .paths import evaluate_server_path_access, path_is_under
from .projection import get_job_or_raise
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
            if stripped:
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

def _count_jsonl_rows(path: Path) -> int:
    count, _, _ = _count_jsonl_rows_with_relative_paths(path)
    return count

def _validate_json_file(path: Path) -> str | None:
    try:
        json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return "file_unreadable"
    except json.JSONDecodeError:
        return "malformed_json"
    return None

def _append_manifest_integrity_issue_sample(
    samples: list[Any],
    issue: Any,
    *,
    sample_limit: int | None = None,
) -> None:
    resolved_limit = (
        sample_limit
        if sample_limit is not None
        else legacy_control_limits().manifest_integrity_issue_sample_limit
    )
    if resolved_limit <= 0:
        return
    if len(samples) < resolved_limit:
        samples.append(issue)

def _manifest_integrity_issue_samples(
    report: ManifestIntegrityResponse,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    samples: list[dict[str, Any]] = []
    for issue in report.bad_scan_units:
        samples.append(
            {
                "kind": "scan_unit",
                "scan_unit_id": issue.scan_unit_id,
                "path": issue.path,
                "manifest_path": issue.manifest_path,
                "expected_file_count": issue.expected_file_count,
                "actual_file_count": issue.actual_file_count,
                "reason": issue.reason,
            }
        )
        if len(samples) >= limit:
            return samples
    for issue in report.bad_shards:
        samples.append(
            {
                "kind": "shard",
                "shard_id": issue.shard_id,
                "shard_index": issue.shard_index,
                "shard_path": issue.shard_path,
                "expected_file_count": issue.expected_file_count,
                "actual_file_count": issue.actual_file_count,
                "reason": issue.reason,
            }
        )
        if len(samples) >= limit:
            return samples
    return samples

def _manifest_integrity_freeze_summary(
    report: ManifestIntegrityResponse,
    *,
    limits: ControlLimits | None = None,
) -> dict[str, Any]:
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    issue_sample_limit = min(
        max(control_limits.manifest_integrity_issue_sample_limit, 0),
        5,
    )
    issue_count = report.bad_scan_unit_count + report.bad_shard_count
    if not report.ok:
        if report.scan_unit_count > 0:
            if (
                not report.scan_unit_manifest_count_matches
                or not report.scan_unit_manifest_total_bytes_matches
            ):
                issue_count += 1
        else:
            if (
                not report.manifest_file_exists
                or not report.manifest_file_count_matches
                or not report.manifest_total_bytes_matches
                or report.manifest_error is not None
            ):
                issue_count += 1
            if (
                report.meta_file_exists is False
                or report.meta_error is not None
                or (report.meta_path is not None and not report.meta_file_count_matches)
                or (
                    report.meta_path is not None
                    and report.meta_actual_total_bytes is not None
                    and not report.meta_total_bytes_matches
                )
            ):
                issue_count += 1
        if not report.shard_file_count_matches_manifest:
            issue_count += 1

    return {
        "integrity_ok": report.ok,
        "integrity_status": report.status,
        "integrity_manifest_file_exists": report.manifest_file_exists,
        "integrity_manifest_file_count_matches": report.manifest_file_count_matches,
        "integrity_manifest_total_bytes_matches": report.manifest_total_bytes_matches,
        "integrity_meta_file_count_matches": report.meta_file_count_matches,
        "integrity_meta_total_bytes_matches": report.meta_total_bytes_matches,
        "integrity_scan_unit_count": report.scan_unit_count,
        "integrity_scan_unit_manifest_count_matches": report.scan_unit_manifest_count_matches,
        "integrity_scan_unit_manifest_total_bytes_matches": report.scan_unit_manifest_total_bytes_matches,
        "integrity_shard_count": report.shard_count,
        "integrity_shard_file_count_matches_manifest": report.shard_file_count_matches_manifest,
        "integrity_bad_scan_unit_count": report.bad_scan_unit_count,
        "integrity_bad_shard_count": report.bad_shard_count,
        "integrity_issue_count": issue_count,
        "integrity_issue_samples": _manifest_integrity_issue_samples(
            report,
            limit=issue_sample_limit,
        ),
    }

def path_is_under_worker_shared_root(session: Session, path: str | None) -> bool:
    if not path:
        return False
    servers = session.execute(
        select(Server)
        .where(Server.archived_at.is_(None))
        .where(Server.id != POOL_SERVER_ID)
    ).scalars()
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        for item in capabilities.get("shared_paths") or []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            if item.get("exists") is False:
                continue
            if path_is_under(str(item["path"]), path):
                return True
    return False

def _server_can_read_path(server: Server, path: str | None) -> bool:
    if not path:
        return False
    access = evaluate_server_path_access(server, path)
    return bool(access.get("can_access"))

def __bounded_manifest_integrity_issues(
    value: Any,
    *,
    limit: int,
) -> Any:
    if isinstance(value, list):
        return value[:limit]
    return value

def _manifest_worker_report_payload(
    report: ManifestIntegrityResponse,
    *,
    limits: ControlLimits | None = None,
) -> dict[str, Any]:
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    issue_sample_limit = max(
        control_limits.manifest_integrity_issue_sample_limit,
        0,
    )
    if hasattr(report, "model_dump"):
        payload = report.model_dump(mode="json")
    else:
        payload = report.dict()
    payload["bad_scan_units"] = __bounded_manifest_integrity_issues(
        payload.get("bad_scan_units", []),
        limit=issue_sample_limit,
    )
    payload["bad_shards"] = __bounded_manifest_integrity_issues(
        payload.get("bad_shards", []),
        limit=issue_sample_limit,
    )
    return payload

def _load_worker_integrity_report(
    manifest: Manifest,
    *,
    limits: ControlLimits | None = None,
) -> ManifestIntegrityResponse | None:
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    issue_sample_limit = max(
        control_limits.manifest_integrity_issue_sample_limit,
        0,
    )
    if not manifest.worker_integrity_report_json:
        return None
    try:
        payload = json_loads_object(manifest.worker_integrity_report_json)
    except json.JSONDecodeError:
        return None
    if not payload:
        return None
    payload = dict(payload)
    payload["bad_scan_units"] = __bounded_manifest_integrity_issues(
        payload.get("bad_scan_units", []),
        limit=issue_sample_limit,
    )
    payload["bad_shards"] = __bounded_manifest_integrity_issues(
        payload.get("bad_shards", []),
        limit=issue_sample_limit,
    )
    payload["source"] = "worker"
    payload["checked_by_server_id"] = manifest.worker_integrity_server_id
    payload["checked_at"] = manifest.worker_integrity_finished_at
    payload["worker_integrity_status"] = manifest.worker_integrity_status
    try:
        return ManifestIntegrityResponse(**payload)
    except ValueError:
        return None

def request_worker_manifest_integrity_check(
    session: Session,
    job_id: str,
) -> ManifestIntegrityWorkerRequestResponse:
    get_job_or_raise(session, job_id)
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if manifest is None:
        return ManifestIntegrityWorkerRequestResponse(
            job_id=job_id,
            manifest_id=None,
            worker_integrity_status="missing_manifest",
            requested_at=None,
        )
    now = utcnow()
    policy.request_worker_integrity(
        manifest,
        requested_at=now,
    )
    session.flush()
    return ManifestIntegrityWorkerRequestResponse(
        job_id=job_id,
        manifest_id=manifest.id,
        worker_integrity_status="pending",
        requested_at=now,
    )

def claim_worker_manifest_integrity_check(
    session: Session,
    server_id: str,
) -> ManifestIntegrityWorkerTask | None:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return None
    manifests = session.execute(
        select(Manifest)
        .where(Manifest.worker_integrity_status == "pending")
        .order_by(Manifest.worker_integrity_requested_at.asc(), Manifest.id.asc())
    ).scalars().all()
    for manifest in manifests:
        if not _server_can_read_path(server, manifest.manifest_path):
            continue
        shards = session.execute(
            select(WorkShard)
            .where(WorkShard.manifest_id == manifest.id)
            .order_by(WorkShard.shard_index.asc())
        ).scalars().all()
        now = utcnow()
        policy.claim_worker_integrity(
            manifest,
            server_id=server_id,
            started_at=now,
        )
        session.flush()
        return ManifestIntegrityWorkerTask(
            job_id=manifest.job_id,
            manifest_id=manifest.id,
            manifest_path=manifest.manifest_path,
            meta_path=manifest.meta_path,
            manifest_expected_file_count=int(manifest.file_count or 0),
            manifest_expected_total_bytes=int(manifest.total_bytes or 0),
            shards=[
                ManifestIntegrityWorkerShardTask(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=int(shard.file_count or 0),
                )
                for shard in shards
            ],
        )
    return None

def complete_worker_manifest_integrity_check(
    session: Session,
    manifest_id: int,
    server_id: str,
    request: ManifestIntegrityWorkerCompleteRequest,
    *,
    limits: ControlLimits | None = None,
) -> ManifestIntegrityWorkerRequestResponse:
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    manifest = session.get(Manifest, manifest_id)
    if manifest is None:
        raise ValueError(f"Unknown manifest {manifest_id}")
    if manifest.worker_integrity_server_id not in (None, server_id):
        raise ValueError(
            f"Manifest integrity check {manifest_id} is assigned to {manifest.worker_integrity_server_id}"
        )
    report = request.report
    if report.manifest_id not in (None, manifest.id) or report.job_id != manifest.job_id:
        raise ValueError("Manifest integrity report does not match the claimed manifest")
    now = utcnow()
    policy.complete_worker_integrity(
        manifest,
        server_id=server_id,
        finished_at=now,
        ok=report.ok,
        report_json=json.dumps(
            _manifest_worker_report_payload(
                report,
                limits=control_limits,
            ),
            ensure_ascii=False,
            default=str,
        ),
    )
    session.flush()
    return ManifestIntegrityWorkerRequestResponse(
        job_id=manifest.job_id,
        manifest_id=manifest.id,
        worker_integrity_status=manifest.worker_integrity_status or "unknown",
        requested_at=manifest.worker_integrity_requested_at,
    )

def get_manifest_integrity_report(
    session: Session,
    job_id: str,
    *,
    limits: ControlLimits | None = None,
) -> ManifestIntegrityResponse:
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    issue_sample_limit = max(
        control_limits.manifest_integrity_issue_sample_limit,
        0,
    )

    def append_issue_sample(samples: list[Any], issue: Any) -> None:
        _append_manifest_integrity_issue_sample(
            samples,
            issue,
            sample_limit=issue_sample_limit,
        )

    get_job_or_raise(session, job_id)
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if manifest is None:
        return ManifestIntegrityResponse(
            job_id=job_id,
            manifest_id=None,
            ok=False,
            status="missing_manifest",
        )

    manifest_path = Path(manifest.manifest_path)
    manifest_file_exists = manifest_path.exists()
    control_cannot_access_manifest = (
        not manifest_file_exists
        and path_is_under_worker_shared_root(session, manifest.manifest_path)
    )
    manifest_actual_file_count: int | None = None
    manifest_file_count_matches = False
    manifest_expected_total_bytes = int(manifest.total_bytes or 0)
    manifest_actual_total_bytes: int | None = None
    manifest_total_bytes_matches = False
    manifest_error: str | None = None
    manifest_relative_paths: set[str] | None = None
    if manifest_file_exists:
        try:
            (
                manifest_actual_file_count,
                manifest_relative_paths,
                manifest_actual_total_bytes,
            ) = _count_jsonl_rows_with_relative_paths(
                manifest_path
            )
            manifest_file_count_matches = manifest_actual_file_count == manifest.file_count
            manifest_total_bytes_matches = (
                manifest_actual_total_bytes == manifest_expected_total_bytes
            )
            if manifest_file_count_matches and not manifest_total_bytes_matches:
                manifest_error = "total_bytes_mismatch"
        except OSError:
            manifest_actual_file_count = None
            manifest_error = "file_unreadable"
        except json.JSONDecodeError:
            manifest_actual_file_count = None
            manifest_error = "malformed_jsonl"
        except InvalidManifestRowError:
            manifest_actual_file_count = None
            manifest_error = "invalid_manifest_row"
        except InvalidManifestRelativePathError:
            manifest_actual_file_count = None
            manifest_error = "invalid_relative_path"
        except DuplicateManifestRelativePathError:
            manifest_actual_file_count = None
            manifest_error = "duplicate_relative_path"

    meta_file_exists: bool | None = None
    meta_error: str | None = None
    meta_expected_file_count = int(manifest.file_count or 0)
    meta_actual_file_count: int | None = None
    meta_file_count_matches = False
    meta_expected_total_bytes = int(manifest.total_bytes or 0)
    meta_actual_total_bytes: int | None = None
    meta_total_bytes_matches = False
    if manifest.meta_path:
        meta_path = Path(manifest.meta_path)
        meta_file_exists = meta_path.exists()
        if meta_file_exists:
            try:
                meta_payload = json.loads(meta_path.read_text(encoding="utf-8"))
            except OSError:
                meta_error = "file_unreadable"
            except json.JSONDecodeError:
                meta_error = "malformed_json"
            else:
                if isinstance(meta_payload, dict) and meta_payload.get("file_count") is not None:
                    try:
                        meta_actual_file_count = int(meta_payload["file_count"])
                    except (TypeError, ValueError):
                        meta_error = "file_count_invalid"
                    else:
                        meta_file_count_matches = meta_actual_file_count == meta_expected_file_count
                    if meta_error is None and meta_payload.get("total_bytes") is not None:
                        try:
                            meta_actual_total_bytes = int(meta_payload["total_bytes"])
                        except (TypeError, ValueError):
                            meta_error = "total_bytes_invalid"
                        else:
                            meta_total_bytes_matches = (
                                meta_actual_total_bytes == meta_expected_total_bytes
                            )
                            if not meta_total_bytes_matches:
                                meta_error = "total_bytes_mismatch"
                elif isinstance(meta_payload, dict):
                    meta_error = "file_count_missing"
                else:
                    meta_error = "malformed_json"

    if control_cannot_access_manifest and int(
        session.execute(
            select(func.count(ScanUnit.id))
            .where(ScanUnit.job_id == job_id)
            .where(ScanUnit.status == "succeeded")
        ).scalar_one()
        or 0
    ) == 0:
        worker_report = _load_worker_integrity_report(
            manifest,
            limits=control_limits,
        )
        if worker_report is not None:
            return worker_report
        shard_count = int(
            session.execute(
                select(func.count(WorkShard.id)).where(WorkShard.job_id == job_id)
            ).scalar_one()
            or 0
        )
        shard_expected_file_count = int(
            session.execute(
                select(func.coalesce(func.sum(WorkShard.file_count), 0)).where(
                    WorkShard.job_id == job_id
                )
            ).scalar_one()
            or 0
        )
        return ManifestIntegrityResponse(
            job_id=job_id,
            manifest_id=manifest.id,
            ok=False,
            status="not_accessible_from_control",
            manifest_path=manifest.manifest_path,
            manifest_file_exists=False,
            manifest_expected_file_count=manifest.file_count,
            manifest_file_count_matches=False,
            manifest_expected_total_bytes=manifest.total_bytes,
            manifest_total_bytes_matches=False,
            worker_integrity_status=manifest.worker_integrity_status,
            meta_path=manifest.meta_path,
            meta_file_exists=False if manifest.meta_path else None,
            meta_expected_file_count=int(manifest.file_count or 0),
            meta_file_count_matches=False,
            meta_expected_total_bytes=int(manifest.total_bytes or 0),
            meta_total_bytes_matches=False,
            shard_count=shard_count,
            shard_expected_file_count=shard_expected_file_count,
            shard_reference_file_count=int(manifest.file_count or 0),
            shard_file_count_matches_manifest=shard_expected_file_count == int(manifest.file_count or 0),
        )

    bad_scan_unit_count = 0
    bad_scan_units: list[ManifestIntegrityScanUnitIssue] = []
    scan_units = session.execute(
        select(ScanUnit)
        .where(ScanUnit.job_id == job_id)
        .where(ScanUnit.status == "succeeded")
        .order_by(ScanUnit.id.asc())
    ).scalars().all()
    scan_unit_expected_file_count = sum(int(unit.file_count or 0) for unit in scan_units)
    scan_unit_expected_total_bytes = sum(int(unit.total_bytes or 0) for unit in scan_units)
    scan_unit_actual_file_count = 0
    scan_unit_actual_total_bytes = 0
    scan_unit_actual_count_known = True
    scan_unit_relative_paths: set[str] = set()
    for unit in scan_units:
        if not unit.manifest_path:
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=None,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="manifest_path_missing",
                )
            )
            scan_unit_actual_count_known = False
            continue
        unit_manifest_path = Path(unit.manifest_path)
        if not unit_manifest_path.exists():
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="file_missing",
                )
            )
            scan_unit_actual_count_known = False
            continue
        try:
            (
                unit_actual_file_count,
                unit_relative_paths,
                unit_actual_total_bytes,
            ) = _count_jsonl_rows_with_relative_paths(
                unit_manifest_path
            )
        except OSError:
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="file_unreadable",
                )
            )
            scan_unit_actual_count_known = False
            continue
        except json.JSONDecodeError:
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="malformed_jsonl",
                )
            )
            scan_unit_actual_count_known = False
            continue
        except InvalidManifestRowError:
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="invalid_manifest_row",
                )
            )
            scan_unit_actual_count_known = False
            continue
        except InvalidManifestRelativePathError:
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="invalid_relative_path",
                )
            )
            scan_unit_actual_count_known = False
            continue
        except DuplicateManifestRelativePathError:
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="duplicate_relative_path",
                )
            )
            scan_unit_actual_count_known = False
            continue
        if scan_unit_relative_paths.intersection(unit_relative_paths):
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=None,
                    reason="duplicate_relative_path",
                )
            )
            scan_unit_actual_count_known = False
            continue
        scan_unit_relative_paths.update(unit_relative_paths)
        scan_unit_actual_file_count += unit_actual_file_count
        if unit_actual_file_count != unit.file_count:
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=unit_actual_file_count,
                    reason="file_count_mismatch",
                )
            )
        if unit_actual_total_bytes != int(unit.total_bytes or 0):
            bad_scan_unit_count += 1
            append_issue_sample(
                bad_scan_units,
                ManifestIntegrityScanUnitIssue(
                    scan_unit_id=unit.id,
                    path=unit.path,
                    manifest_path=unit.manifest_path,
                    expected_file_count=unit.file_count,
                    actual_file_count=unit_actual_file_count,
                    reason="total_bytes_mismatch",
                )
            )
            scan_unit_actual_count_known = False
            continue
        scan_unit_actual_total_bytes += unit_actual_total_bytes
        if unit.meta_path:
            unit_meta_path = Path(unit.meta_path)
            if not unit_meta_path.exists():
                bad_scan_unit_count += 1
                append_issue_sample(
                    bad_scan_units,
                    ManifestIntegrityScanUnitIssue(
                        scan_unit_id=unit.id,
                        path=unit.path,
                        manifest_path=unit.meta_path,
                        expected_file_count=unit.file_count,
                        actual_file_count=unit_actual_file_count,
                        reason="meta_file_missing",
                    )
                )
            else:
                try:
                    unit_meta_payload = json.loads(unit_meta_path.read_text(encoding="utf-8"))
                except OSError:
                    unit_meta_error = "file_unreadable"
                    unit_meta_payload = None
                except json.JSONDecodeError:
                    unit_meta_error = "malformed_json"
                    unit_meta_payload = None
                else:
                    unit_meta_error = None if isinstance(unit_meta_payload, dict) else "malformed_json"
                if unit_meta_error:
                    unit_meta_reason = (
                        "meta_file_malformed"
                        if unit_meta_error == "malformed_json"
                        else "meta_file_unreadable"
                    )
                    bad_scan_unit_count += 1
                    append_issue_sample(
                        bad_scan_units,
                        ManifestIntegrityScanUnitIssue(
                            scan_unit_id=unit.id,
                            path=unit.path,
                            manifest_path=unit.meta_path,
                            expected_file_count=unit.file_count,
                            actual_file_count=unit_actual_file_count,
                            reason=unit_meta_reason,
                        )
                    )
                elif isinstance(unit_meta_payload, dict) and unit_meta_payload.get("file_count") is not None:
                    try:
                        unit_meta_file_count = int(unit_meta_payload["file_count"])
                    except (TypeError, ValueError):
                        bad_scan_unit_count += 1
                        append_issue_sample(
                            bad_scan_units,
                            ManifestIntegrityScanUnitIssue(
                                scan_unit_id=unit.id,
                                path=unit.path,
                                manifest_path=unit.meta_path,
                                expected_file_count=unit.file_count,
                                actual_file_count=unit_actual_file_count,
                                reason="meta_file_count_invalid",
                            )
                        )
                    else:
                        if unit_meta_file_count != int(unit.file_count or 0):
                            bad_scan_unit_count += 1
                            append_issue_sample(
                                bad_scan_units,
                                ManifestIntegrityScanUnitIssue(
                                    scan_unit_id=unit.id,
                                    path=unit.path,
                                    manifest_path=unit.meta_path,
                                    expected_file_count=unit.file_count,
                                    actual_file_count=unit_meta_file_count,
                                    reason="meta_file_count_mismatch",
                                )
                            )
                if isinstance(unit_meta_payload, dict) and unit_meta_payload.get("total_bytes") is not None:
                    try:
                        unit_meta_total_bytes = int(unit_meta_payload["total_bytes"])
                    except (TypeError, ValueError):
                        bad_scan_unit_count += 1
                        append_issue_sample(
                            bad_scan_units,
                            ManifestIntegrityScanUnitIssue(
                                scan_unit_id=unit.id,
                                path=unit.path,
                                manifest_path=unit.meta_path,
                                expected_file_count=unit.file_count,
                                actual_file_count=unit_actual_file_count,
                                reason="meta_total_bytes_invalid",
                            )
                        )
                    else:
                        if unit_meta_total_bytes != int(unit.total_bytes or 0):
                            bad_scan_unit_count += 1
                            append_issue_sample(
                                bad_scan_units,
                                ManifestIntegrityScanUnitIssue(
                                    scan_unit_id=unit.id,
                                    path=unit.path,
                                    manifest_path=unit.meta_path,
                                    expected_file_count=unit.file_count,
                                    actual_file_count=unit_actual_file_count,
                                    reason="meta_total_bytes_mismatch",
                                )
                            )

    has_distributed_scan_units = bool(scan_units)
    scan_unit_manifest_actual_total = (
        scan_unit_actual_file_count if scan_unit_actual_count_known else None
    )
    scan_unit_manifest_actual_total_bytes = (
        scan_unit_actual_total_bytes if scan_unit_actual_count_known else None
    )
    scan_unit_manifest_count_matches = (
        has_distributed_scan_units
        and scan_unit_actual_count_known
        and scan_unit_actual_file_count == scan_unit_expected_file_count
        and scan_unit_expected_file_count == int(manifest.file_count or 0)
        and bad_scan_unit_count == 0
    )
    scan_unit_manifest_total_bytes_matches = (
        has_distributed_scan_units
        and scan_unit_actual_count_known
        and scan_unit_actual_total_bytes == scan_unit_expected_total_bytes
        and scan_unit_expected_total_bytes == int(manifest.total_bytes or 0)
        and bad_scan_unit_count == 0
    )

    bad_shard_count = 0
    bad_shards: list[ManifestIntegrityShardIssue] = []
    shards = session.execute(
        select(WorkShard)
        .where(WorkShard.job_id == job_id)
        .order_by(WorkShard.shard_index.asc())
    ).scalars().all()
    shard_expected_file_count = sum(int(shard.file_count or 0) for shard in shards)
    shard_relative_paths: set[str] = set()
    for shard in shards:
        shard_path = Path(shard.shard_path)
        if not shard_path.exists():
            bad_shard_count += 1
            append_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="file_missing",
                )
            )
            continue
        try:
            (
                actual_file_count,
                shard_file_relative_paths,
                _shard_total_bytes,
            ) = _count_jsonl_rows_with_relative_paths(
                shard_path
            )
        except OSError:
            bad_shard_count += 1
            append_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="file_unreadable",
                )
            )
            continue
        except json.JSONDecodeError:
            bad_shard_count += 1
            append_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="malformed_jsonl",
                )
            )
            continue
        except InvalidManifestRowError:
            bad_shard_count += 1
            append_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="invalid_manifest_row",
                )
            )
            continue
        except InvalidManifestRelativePathError:
            bad_shard_count += 1
            append_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="invalid_relative_path",
                )
            )
            continue
        except DuplicateManifestRelativePathError:
            bad_shard_count += 1
            append_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="duplicate_relative_path",
                )
            )
            continue
        if shard_relative_paths.intersection(shard_file_relative_paths):
            bad_shard_count += 1
            append_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="duplicate_relative_path",
                )
            )
            continue
        shard_reference_relative_paths = (
            scan_unit_relative_paths if has_distributed_scan_units else manifest_relative_paths
        )
        if (
            shard_reference_relative_paths is not None
            and shard_file_relative_paths.difference(shard_reference_relative_paths)
        ):
            bad_shard_count += 1
            append_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=None,
                    reason="relative_path_not_in_manifest",
                )
            )
            continue
        shard_relative_paths.update(shard_file_relative_paths)
        if actual_file_count != shard.file_count:
            bad_shard_count += 1
            append_issue_sample(
                bad_shards,
                ManifestIntegrityShardIssue(
                    shard_id=shard.id,
                    shard_index=shard.shard_index,
                    shard_path=shard.shard_path,
                    expected_file_count=shard.file_count,
                    actual_file_count=actual_file_count,
                    reason="file_count_mismatch",
                )
            )

    if has_distributed_scan_units:
        manifest_ok = scan_unit_manifest_count_matches
        manifest_total_ok = scan_unit_manifest_total_bytes_matches
        meta_ok = True
        shard_reference_file_count = scan_unit_expected_file_count
    else:
        manifest_ok = manifest_file_exists and manifest_file_count_matches
        manifest_total_ok = (
            manifest_file_exists
            and manifest_error is None
            and manifest_total_bytes_matches
        )
        meta_ok = (
            meta_file_exists is not False
            and meta_error is None
            and (manifest.meta_path is None or meta_file_count_matches)
            and (
                manifest.meta_path is None
                or meta_actual_total_bytes is None
                or meta_total_bytes_matches
            )
        )
        shard_reference_file_count = int(manifest.file_count or 0)
    shard_file_count_matches_manifest = shard_expected_file_count == shard_reference_file_count
    ok = (
        manifest_ok
        and manifest_total_ok
        and meta_ok
        and shard_file_count_matches_manifest
        and bad_scan_unit_count == 0
        and bad_shard_count == 0
    )
    return ManifestIntegrityResponse(
        job_id=job_id,
        manifest_id=manifest.id,
        ok=ok,
        status="ok" if ok else "failed",
        manifest_path=manifest.manifest_path,
        manifest_file_exists=manifest_file_exists,
        manifest_expected_file_count=manifest.file_count,
        manifest_actual_file_count=manifest_actual_file_count,
        manifest_file_count_matches=manifest_file_count_matches,
        manifest_expected_total_bytes=manifest_expected_total_bytes,
        manifest_actual_total_bytes=manifest_actual_total_bytes,
        manifest_total_bytes_matches=manifest_total_bytes_matches,
        manifest_error=manifest_error,
        meta_path=manifest.meta_path,
        meta_file_exists=meta_file_exists,
        meta_error=meta_error,
        meta_expected_file_count=meta_expected_file_count,
        meta_actual_file_count=meta_actual_file_count,
        meta_file_count_matches=meta_file_count_matches,
        meta_expected_total_bytes=meta_expected_total_bytes,
        meta_actual_total_bytes=meta_actual_total_bytes,
        meta_total_bytes_matches=meta_total_bytes_matches,
        scan_unit_count=len(scan_units),
        scan_unit_manifest_expected_file_count=scan_unit_expected_file_count,
        scan_unit_manifest_actual_file_count=scan_unit_manifest_actual_total,
        scan_unit_manifest_count_matches=scan_unit_manifest_count_matches,
        scan_unit_manifest_expected_total_bytes=scan_unit_expected_total_bytes,
        scan_unit_manifest_actual_total_bytes=scan_unit_manifest_actual_total_bytes,
        scan_unit_manifest_total_bytes_matches=scan_unit_manifest_total_bytes_matches,
        bad_scan_unit_count=bad_scan_unit_count,
        bad_scan_units=bad_scan_units,
        shard_count=len(shards),
        shard_expected_file_count=shard_expected_file_count,
        shard_reference_file_count=shard_reference_file_count,
        shard_file_count_matches_manifest=shard_file_count_matches_manifest,
        bad_shard_count=bad_shard_count,
        bad_shards=bad_shards,
    )

load_worker_integrity_report = _load_worker_integrity_report
manifest_integrity_freeze_summary = _manifest_integrity_freeze_summary
bounded_manifest_integrity_issues = __bounded_manifest_integrity_issues

__all__ = [name for name in globals() if not name.startswith("__")]
