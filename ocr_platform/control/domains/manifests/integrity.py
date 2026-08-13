"""Manifest integrity validation, worker evidence, and read projections."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ocr_platform.manifest.models import ManifestItem
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...limits import ControlLimits, legacy_control_limits
from ...models import Manifest, ScanUnit, Server, WorkShard
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


@dataclass(frozen=True)
class _ManifestFileEvidence:
    exists: bool
    actual_file_count: int | None
    file_count_matches: bool
    expected_total_bytes: int
    actual_total_bytes: int | None
    total_bytes_matches: bool
    error: str | None
    relative_paths: set[str] | None


@dataclass(frozen=True)
class _ManifestMetaEvidence:
    exists: bool | None
    error: str | None
    expected_file_count: int
    actual_file_count: int | None
    file_count_matches: bool
    expected_total_bytes: int
    actual_total_bytes: int | None
    total_bytes_matches: bool


@dataclass(frozen=True)
class _ScanUnitAudit:
    count: int
    expected_file_count: int
    actual_file_count: int | None
    file_count_matches: bool
    expected_total_bytes: int
    actual_total_bytes: int | None
    total_bytes_matches: bool
    relative_paths: set[str]
    bad_count: int
    bad_samples: list[ManifestIntegrityScanUnitIssue]


@dataclass(frozen=True)
class _ShardAudit:
    count: int
    expected_file_count: int
    bad_count: int
    bad_samples: list[ManifestIntegrityShardIssue]


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


def _read_manifest_jsonl(
    path: Path,
) -> tuple[tuple[int, set[str], int] | None, str | None]:
    try:
        return _count_jsonl_rows_with_relative_paths(path), None
    except OSError:
        return None, "file_unreadable"
    except json.JSONDecodeError:
        return None, "malformed_jsonl"
    except InvalidManifestRowError:
        return None, "invalid_manifest_row"
    except InvalidManifestRelativePathError:
        return None, "invalid_relative_path"
    except DuplicateManifestRelativePathError:
        return None, "duplicate_relative_path"


def _read_json_object(
    path: Path,
) -> tuple[dict[str, Any] | None, str | None]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError:
        return None, "file_unreadable"
    except json.JSONDecodeError:
        return None, "malformed_json"
    if not isinstance(payload, dict):
        return None, "malformed_json"
    return payload, None

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

def _read_manifest_file_evidence(manifest: Manifest) -> _ManifestFileEvidence:
    path = Path(manifest.manifest_path)
    exists = path.exists()
    expected_total_bytes = int(manifest.total_bytes or 0)
    rows, error = _read_manifest_jsonl(path) if exists else (None, None)
    if rows is None:
        return _ManifestFileEvidence(
            exists=exists,
            actual_file_count=None,
            file_count_matches=False,
            expected_total_bytes=expected_total_bytes,
            actual_total_bytes=None,
            total_bytes_matches=False,
            error=error,
            relative_paths=None,
        )
    actual_file_count, relative_paths, actual_total_bytes = rows
    file_count_matches = actual_file_count == manifest.file_count
    total_bytes_matches = actual_total_bytes == expected_total_bytes
    if file_count_matches and not total_bytes_matches:
        error = "total_bytes_mismatch"
    return _ManifestFileEvidence(
        exists=True,
        actual_file_count=actual_file_count,
        file_count_matches=file_count_matches,
        expected_total_bytes=expected_total_bytes,
        actual_total_bytes=actual_total_bytes,
        total_bytes_matches=total_bytes_matches,
        error=error,
        relative_paths=relative_paths,
    )


def _read_manifest_meta_evidence(manifest: Manifest) -> _ManifestMetaEvidence:
    expected_file_count = int(manifest.file_count or 0)
    expected_total_bytes = int(manifest.total_bytes or 0)
    exists: bool | None = None
    error: str | None = None
    actual_file_count: int | None = None
    actual_total_bytes: int | None = None
    file_count_matches = False
    total_bytes_matches = False
    if manifest.meta_path:
        path = Path(manifest.meta_path)
        exists = path.exists()
        payload, error = _read_json_object(path) if exists else (None, None)
        if payload is not None:
            if payload.get("file_count") is None:
                error = "file_count_missing"
            else:
                try:
                    actual_file_count = int(payload["file_count"])
                except (TypeError, ValueError):
                    error = "file_count_invalid"
                else:
                    file_count_matches = actual_file_count == expected_file_count
                if error is None and payload.get("total_bytes") is not None:
                    try:
                        actual_total_bytes = int(payload["total_bytes"])
                    except (TypeError, ValueError):
                        error = "total_bytes_invalid"
                    else:
                        total_bytes_matches = actual_total_bytes == expected_total_bytes
                        if not total_bytes_matches:
                            error = "total_bytes_mismatch"
    return _ManifestMetaEvidence(
        exists=exists,
        error=error,
        expected_file_count=expected_file_count,
        actual_file_count=actual_file_count,
        file_count_matches=file_count_matches,
        expected_total_bytes=expected_total_bytes,
        actual_total_bytes=actual_total_bytes,
        total_bytes_matches=total_bytes_matches,
    )


def _worker_only_integrity_response(
    session: Session,
    job_id: str,
    manifest: Manifest,
    evidence: _ManifestFileEvidence,
    *,
    limits: ControlLimits,
) -> ManifestIntegrityResponse | None:
    if evidence.exists or not path_is_under_worker_shared_root(
        session, manifest.manifest_path
    ):
        return None
    succeeded_scan_units = int(
        session.execute(
            select(func.count(ScanUnit.id))
            .where(ScanUnit.job_id == job_id)
            .where(ScanUnit.status == "succeeded")
        ).scalar_one()
        or 0
    )
    if succeeded_scan_units:
        return None
    worker_report = _load_worker_integrity_report(manifest, limits=limits)
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
    reference_count = int(manifest.file_count or 0)
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
        meta_expected_file_count=reference_count,
        meta_file_count_matches=False,
        meta_expected_total_bytes=int(manifest.total_bytes or 0),
        meta_total_bytes_matches=False,
        shard_count=shard_count,
        shard_expected_file_count=shard_expected_file_count,
        shard_reference_file_count=reference_count,
        shard_file_count_matches_manifest=shard_expected_file_count == reference_count,
    )


def _scan_unit_issue(
    unit: ScanUnit,
    reason: str,
    *,
    actual_file_count: int | None,
    manifest_path: str | None = None,
) -> ManifestIntegrityScanUnitIssue:
    return ManifestIntegrityScanUnitIssue(
        scan_unit_id=unit.id,
        path=unit.path,
        manifest_path=unit.manifest_path if manifest_path is None else manifest_path,
        expected_file_count=unit.file_count,
        actual_file_count=actual_file_count,
        reason=reason,
    )


def _scan_unit_meta_issues(
    unit: ScanUnit,
    actual_file_count: int,
) -> list[ManifestIntegrityScanUnitIssue]:
    if not unit.meta_path:
        return []
    path = Path(unit.meta_path)
    if not path.exists():
        return [
            _scan_unit_issue(
                unit,
                "meta_file_missing",
                actual_file_count=actual_file_count,
                manifest_path=unit.meta_path,
            )
        ]
    payload, error = _read_json_object(path)
    issues: list[ManifestIntegrityScanUnitIssue] = []
    if error:
        reason = "meta_file_malformed" if error == "malformed_json" else "meta_file_unreadable"
        issues.append(
            _scan_unit_issue(
                unit,
                reason,
                actual_file_count=actual_file_count,
                manifest_path=unit.meta_path,
            )
        )
    elif payload.get("file_count") is not None:
        try:
            meta_file_count = int(payload["file_count"])
        except (TypeError, ValueError):
            issues.append(
                _scan_unit_issue(
                    unit,
                    "meta_file_count_invalid",
                    actual_file_count=actual_file_count,
                    manifest_path=unit.meta_path,
                )
            )
        else:
            if meta_file_count != int(unit.file_count or 0):
                issues.append(
                    _scan_unit_issue(
                        unit,
                        "meta_file_count_mismatch",
                        actual_file_count=meta_file_count,
                        manifest_path=unit.meta_path,
                    )
                )
    if payload is not None and payload.get("total_bytes") is not None:
        try:
            meta_total_bytes = int(payload["total_bytes"])
        except (TypeError, ValueError):
            reason = "meta_total_bytes_invalid"
        else:
            reason = (
                "meta_total_bytes_mismatch"
                if meta_total_bytes != int(unit.total_bytes or 0)
                else None
            )
        if reason is not None:
            issues.append(
                _scan_unit_issue(
                    unit,
                    reason,
                    actual_file_count=actual_file_count,
                    manifest_path=unit.meta_path,
                )
            )
    return issues


def _audit_scan_units(
    session: Session,
    job_id: str,
    manifest: Manifest,
    append_sample: Callable[[list[Any], Any], None],
) -> _ScanUnitAudit:
    units = session.execute(
        select(ScanUnit)
        .where(ScanUnit.job_id == job_id)
        .where(ScanUnit.status == "succeeded")
        .order_by(ScanUnit.id.asc())
    ).scalars().all()
    expected_count = sum(int(unit.file_count or 0) for unit in units)
    expected_bytes = sum(int(unit.total_bytes or 0) for unit in units)
    actual_count = 0
    actual_bytes = 0
    actual_known = True
    relative_paths: set[str] = set()
    bad_count = 0
    bad_samples: list[ManifestIntegrityScanUnitIssue] = []
    for unit in units:
        rows: tuple[int, set[str], int] | None = None
        error: str | None = None
        if not unit.manifest_path:
            error = "manifest_path_missing"
        else:
            path = Path(unit.manifest_path)
            if not path.exists():
                error = "file_missing"
            else:
                rows, error = _read_manifest_jsonl(path)
        if rows is None:
            bad_count += 1
            append_sample(
                bad_samples,
                _scan_unit_issue(
                    unit,
                    error or "file_unreadable",
                    actual_file_count=None,
                ),
            )
            actual_known = False
            continue
        unit_count, unit_paths, unit_bytes = rows
        if relative_paths.intersection(unit_paths):
            bad_count += 1
            append_sample(
                bad_samples,
                _scan_unit_issue(unit, "duplicate_relative_path", actual_file_count=None),
            )
            actual_known = False
            continue
        relative_paths.update(unit_paths)
        actual_count += unit_count
        if unit_count != unit.file_count:
            bad_count += 1
            append_sample(
                bad_samples,
                _scan_unit_issue(unit, "file_count_mismatch", actual_file_count=unit_count),
            )
        if unit_bytes != int(unit.total_bytes or 0):
            bad_count += 1
            append_sample(
                bad_samples,
                _scan_unit_issue(unit, "total_bytes_mismatch", actual_file_count=unit_count),
            )
            actual_known = False
            continue
        actual_bytes += unit_bytes
        for issue in _scan_unit_meta_issues(unit, unit_count):
            bad_count += 1
            append_sample(bad_samples, issue)
    has_units = bool(units)
    return _ScanUnitAudit(
        count=len(units),
        expected_file_count=expected_count,
        actual_file_count=actual_count if actual_known else None,
        file_count_matches=(
            has_units
            and actual_known
            and actual_count == expected_count
            and expected_count == int(manifest.file_count or 0)
            and bad_count == 0
        ),
        expected_total_bytes=expected_bytes,
        actual_total_bytes=actual_bytes if actual_known else None,
        total_bytes_matches=(
            has_units
            and actual_known
            and actual_bytes == expected_bytes
            and expected_bytes == int(manifest.total_bytes or 0)
            and bad_count == 0
        ),
        relative_paths=relative_paths,
        bad_count=bad_count,
        bad_samples=bad_samples,
    )


def _shard_issue(
    shard: WorkShard,
    reason: str,
    *,
    actual_file_count: int | None,
) -> ManifestIntegrityShardIssue:
    return ManifestIntegrityShardIssue(
        shard_id=shard.id,
        shard_index=shard.shard_index,
        shard_path=shard.shard_path,
        expected_file_count=shard.file_count,
        actual_file_count=actual_file_count,
        reason=reason,
    )


def _audit_shards(
    session: Session,
    job_id: str,
    reference_paths: set[str] | None,
    append_sample: Callable[[list[Any], Any], None],
) -> _ShardAudit:
    shards = session.execute(
        select(WorkShard)
        .where(WorkShard.job_id == job_id)
        .order_by(WorkShard.shard_index.asc())
    ).scalars().all()
    expected_count = sum(int(shard.file_count or 0) for shard in shards)
    seen_paths: set[str] = set()
    bad_count = 0
    bad_samples: list[ManifestIntegrityShardIssue] = []
    for shard in shards:
        path = Path(shard.shard_path)
        rows, error = _read_manifest_jsonl(path) if path.exists() else (None, "file_missing")
        if rows is None:
            bad_count += 1
            append_sample(
                bad_samples,
                _shard_issue(
                    shard,
                    error or "file_unreadable",
                    actual_file_count=None,
                ),
            )
            continue
        actual_count, shard_paths, _total_bytes = rows
        if seen_paths.intersection(shard_paths):
            bad_count += 1
            append_sample(
                bad_samples,
                _shard_issue(shard, "duplicate_relative_path", actual_file_count=None),
            )
            continue
        if reference_paths is not None and shard_paths.difference(reference_paths):
            bad_count += 1
            append_sample(
                bad_samples,
                _shard_issue(shard, "relative_path_not_in_manifest", actual_file_count=None),
            )
            continue
        seen_paths.update(shard_paths)
        if actual_count != shard.file_count:
            bad_count += 1
            append_sample(
                bad_samples,
                _shard_issue(shard, "file_count_mismatch", actual_file_count=actual_count),
            )
    return _ShardAudit(
        count=len(shards),
        expected_file_count=expected_count,
        bad_count=bad_count,
        bad_samples=bad_samples,
    )


def _build_manifest_integrity_response(
    job_id: str,
    manifest: Manifest,
    manifest_file: _ManifestFileEvidence,
    meta: _ManifestMetaEvidence,
    scan: _ScanUnitAudit,
    shards: _ShardAudit,
) -> ManifestIntegrityResponse:
    if scan.count:
        manifest_ok = scan.file_count_matches
        manifest_total_ok = scan.total_bytes_matches
        meta_ok = True
        shard_reference_count = scan.expected_file_count
    else:
        manifest_ok = manifest_file.exists and manifest_file.file_count_matches
        manifest_total_ok = (
            manifest_file.exists
            and manifest_file.error is None
            and manifest_file.total_bytes_matches
        )
        meta_ok = (
            meta.exists is not False
            and meta.error is None
            and (manifest.meta_path is None or meta.file_count_matches)
            and (
                manifest.meta_path is None
                or meta.actual_total_bytes is None
                or meta.total_bytes_matches
            )
        )
        shard_reference_count = int(manifest.file_count or 0)
    shard_counts_match = shards.expected_file_count == shard_reference_count
    ok = (
        manifest_ok
        and manifest_total_ok
        and meta_ok
        and shard_counts_match
        and scan.bad_count == 0
        and shards.bad_count == 0
    )
    return ManifestIntegrityResponse(
        job_id=job_id,
        manifest_id=manifest.id,
        ok=ok,
        status="ok" if ok else "failed",
        manifest_path=manifest.manifest_path,
        manifest_file_exists=manifest_file.exists,
        manifest_expected_file_count=manifest.file_count,
        manifest_actual_file_count=manifest_file.actual_file_count,
        manifest_file_count_matches=manifest_file.file_count_matches,
        manifest_expected_total_bytes=manifest_file.expected_total_bytes,
        manifest_actual_total_bytes=manifest_file.actual_total_bytes,
        manifest_total_bytes_matches=manifest_file.total_bytes_matches,
        manifest_error=manifest_file.error,
        meta_path=manifest.meta_path,
        meta_file_exists=meta.exists,
        meta_error=meta.error,
        meta_expected_file_count=meta.expected_file_count,
        meta_actual_file_count=meta.actual_file_count,
        meta_file_count_matches=meta.file_count_matches,
        meta_expected_total_bytes=meta.expected_total_bytes,
        meta_actual_total_bytes=meta.actual_total_bytes,
        meta_total_bytes_matches=meta.total_bytes_matches,
        scan_unit_count=scan.count,
        scan_unit_manifest_expected_file_count=scan.expected_file_count,
        scan_unit_manifest_actual_file_count=scan.actual_file_count,
        scan_unit_manifest_count_matches=scan.file_count_matches,
        scan_unit_manifest_expected_total_bytes=scan.expected_total_bytes,
        scan_unit_manifest_actual_total_bytes=scan.actual_total_bytes,
        scan_unit_manifest_total_bytes_matches=scan.total_bytes_matches,
        bad_scan_unit_count=scan.bad_count,
        bad_scan_units=scan.bad_samples,
        shard_count=shards.count,
        shard_expected_file_count=shards.expected_file_count,
        shard_reference_file_count=shard_reference_count,
        shard_file_count_matches_manifest=shard_counts_match,
        bad_shard_count=shards.bad_count,
        bad_shards=shards.bad_samples,
    )


def _assemble_manifest_integrity_report(
    session: Session,
    job_id: str,
    *,
    limits: ControlLimits | None = None,
) -> ManifestIntegrityResponse:
    control_limits = limits if limits is not None else legacy_control_limits()
    issue_sample_limit = max(control_limits.manifest_integrity_issue_sample_limit, 0)

    def append_sample(samples: list[Any], issue: Any) -> None:
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
    manifest_file = _read_manifest_file_evidence(manifest)
    meta = _read_manifest_meta_evidence(manifest)
    worker_only = _worker_only_integrity_response(
        session,
        job_id,
        manifest,
        manifest_file,
        limits=control_limits,
    )
    if worker_only is not None:
        return worker_only
    scan = _audit_scan_units(session, job_id, manifest, append_sample)
    reference_paths = scan.relative_paths if scan.count else manifest_file.relative_paths
    shards = _audit_shards(session, job_id, reference_paths, append_sample)
    return _build_manifest_integrity_response(
        job_id,
        manifest,
        manifest_file,
        meta,
        scan,
        shards,
    )


def get_manifest_integrity_report(
    session: Session,
    job_id: str,
    *,
    limits: ControlLimits | None = None,
) -> ManifestIntegrityResponse:
    """Read manifest evidence and assemble its public integrity projection."""
    return _assemble_manifest_integrity_report(
        session,
        job_id,
        limits=limits,
    )

load_worker_integrity_report = _load_worker_integrity_report
manifest_integrity_freeze_summary = _manifest_integrity_freeze_summary
bounded_manifest_integrity_issues = __bounded_manifest_integrity_issues

__all__ = [name for name in globals() if not name.startswith("__")]
