from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..contracts import ManifestItem

from .resume import check_output_artifacts


@dataclass(frozen=True)
class ManifestOutputAuditReport:
    manifest_path: str
    output_dir: str
    total_items: int = 0
    ok_items: int = 0
    issue_count: int = 0
    issues_by_category: dict[str, int] = field(default_factory=dict)
    issues_by_failure_category: dict[str, int] = field(default_factory=dict)
    issues_by_error_type: dict[str, int] = field(default_factory=dict)
    issue_samples: list[dict[str, Any]] = field(default_factory=list)
    issue_sample_limit: int = 20
    issue_samples_truncated: bool = False
    audited_items: int = 0
    max_items: int | None = None
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.issue_count == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "manifest_path": self.manifest_path,
            "output_dir": self.output_dir,
            "total_items": self.total_items,
            "audited_items": self.audited_items,
            "max_items": self.max_items,
            "truncated": self.truncated,
            "ok_items": self.ok_items,
            "issue_count": self.issue_count,
            "issues_by_category": self.issues_by_category,
            "issues_by_failure_category": self.issues_by_failure_category,
            "issues_by_error_type": self.issues_by_error_type,
            "issue_samples": self.issue_samples,
            "issue_sample_limit": self.issue_sample_limit,
            "issue_samples_truncated": self.issue_samples_truncated,
        }


class _AuditBuilder:
    def __init__(self, *, manifest_path: Path, output_dir: Path, sample_limit: int) -> None:
        self.manifest_path = manifest_path
        self.output_dir = output_dir
        self.sample_limit = max(sample_limit, 0)
        self.total_items = 0
        self.ok_items = 0
        self.issue_count = 0
        self.truncated = False
        self.issue_samples_truncated = False
        self.issues_by_category: dict[str, int] = {}
        self.issues_by_failure_category: dict[str, int] = {}
        self.issues_by_error_type: dict[str, int] = {}
        self.issue_samples: list[dict[str, Any]] = []

    def add_ok(self) -> None:
        self.ok_items += 1

    def add_issue(
        self,
        *,
        category: str,
        item: ManifestItem | None = None,
        line_number: int | None = None,
        message: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.issue_count += 1
        self.issues_by_category[category] = self.issues_by_category.get(category, 0) + 1
        if extra:
            failure_category = extra.get("sidecar_failure_category")
            if failure_category:
                key = str(failure_category)
                self.issues_by_failure_category[key] = self.issues_by_failure_category.get(key, 0) + 1
            error_type = extra.get("sidecar_error_type")
            if error_type:
                key = str(error_type)
                self.issues_by_error_type[key] = self.issues_by_error_type.get(key, 0) + 1
        if len(self.issue_samples) >= self.sample_limit:
            self.issue_samples_truncated = True
            return
        sample: dict[str, Any] = {"category": category}
        if item is not None:
            sample.update(
                {
                    "input_path": item.input_path,
                    "relative_path": item.relative_path,
                }
            )
        if line_number is not None:
            sample["line_number"] = line_number
        if message:
            sample["message"] = message
        if extra:
            sample.update(extra)
        self.issue_samples.append(sample)

    def build(self, *, max_items: int | None = None) -> ManifestOutputAuditReport:
        return ManifestOutputAuditReport(
            manifest_path=str(self.manifest_path),
            output_dir=str(self.output_dir),
            total_items=self.total_items,
            audited_items=self.total_items,
            ok_items=self.ok_items,
            issue_count=self.issue_count,
            issues_by_category=dict(sorted(self.issues_by_category.items())),
            issues_by_failure_category=dict(sorted(self.issues_by_failure_category.items())),
            issues_by_error_type=dict(sorted(self.issues_by_error_type.items())),
            issue_samples=self.issue_samples,
            issue_sample_limit=self.sample_limit,
            issue_samples_truncated=self.issue_samples_truncated,
            max_items=max_items,
            truncated=self.truncated,
        )


def _console_noop(message: str, level: str = "info") -> None:
    return None


def _safe_relative_path(relative_path: str) -> Path:
    if "\\" in relative_path:
        raise ValueError("relative_path must use POSIX '/' separators")
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("relative_path must be relative and may not contain '..'")
    if not path.name or path.suffix.lower() != ".pdf":
        raise ValueError("relative_path must point to a PDF file")
    return path


def _validate_input_freshness(item: ManifestItem) -> tuple[bool, str | None, str | None]:
    path = Path(item.input_path)
    if not path.exists():
        return False, "input_missing", f"input file missing: {path}"
    try:
        stat = path.stat()
    except OSError as exc:
        return False, "input_invalid", str(exc)
    if int(stat.st_size) != int(item.size_bytes) or int(stat.st_mtime_ns) != int(item.mtime_ns):
        return False, "input_changed", (
            f"input file changed since manifest snapshot: {path} "
            f"(size {stat.st_size}!={item.size_bytes} or mtime {stat.st_mtime_ns}!={item.mtime_ns})"
        )
    return True, None, None


def _read_sidecar_payload(sidecar_path: str) -> dict[str, Any]:
    try:
        payload = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _validate_sidecar_input_snapshot(
    *,
    item: ManifestItem,
    sidecar_path: str,
) -> tuple[bool, str | None, dict[str, Any]]:
    payload = _read_sidecar_payload(sidecar_path)
    sidecar_size = payload.get("input_size_bytes")
    sidecar_mtime = payload.get("input_mtime_ns")
    if sidecar_size is None or sidecar_mtime is None:
        return False, "OCR sidecar is missing input_size_bytes or input_mtime_ns", {
            "sidecar_path": sidecar_path,
            "sidecar_input_size_bytes": sidecar_size,
            "sidecar_input_mtime_ns": sidecar_mtime,
            "manifest_size_bytes": item.size_bytes,
            "manifest_mtime_ns": item.mtime_ns,
        }
    try:
        size_matches = sidecar_size is None or int(sidecar_size) == int(item.size_bytes)
        mtime_matches = sidecar_mtime is None or int(sidecar_mtime) == int(item.mtime_ns)
    except (TypeError, ValueError):
        size_matches = False
        mtime_matches = False
    if size_matches and mtime_matches:
        return True, None, {}
    return False, "OCR sidecar input snapshot does not match manifest row", {
        "sidecar_path": sidecar_path,
        "sidecar_input_size_bytes": sidecar_size,
        "sidecar_input_mtime_ns": sidecar_mtime,
        "manifest_size_bytes": item.size_bytes,
        "manifest_mtime_ns": item.mtime_ns,
    }


def _validate_sidecar_manifest_relative_path(
    *,
    item: ManifestItem,
    sidecar_path: str,
) -> tuple[bool, str | None, dict[str, Any]]:
    payload = _read_sidecar_payload(sidecar_path)
    sidecar_relative_path = payload.get("manifest_relative_path")
    if sidecar_relative_path is None:
        return True, None, {}
    if str(sidecar_relative_path) == item.relative_path:
        return True, None, {}
    return False, "OCR sidecar manifest_relative_path does not match manifest row", {
        "sidecar_path": sidecar_path,
        "sidecar_manifest_relative_path": sidecar_relative_path,
        "manifest_relative_path": item.relative_path,
    }


def _audit_item_output(
    *,
    item: ManifestItem,
    output_dir: Path,
) -> tuple[bool, str | None, str | None, dict[str, Any]]:
    try:
        relative_path = _safe_relative_path(item.relative_path)
    except ValueError as exc:
        return False, "invalid_relative_path", str(exc), {}
    item_output_root = output_dir / relative_path.parent
    report = check_output_artifacts(item_output_root, relative_path.stem, _console_noop)
    if report.ok:
        snapshot_ok, snapshot_message, snapshot_extra = _validate_sidecar_input_snapshot(
            item=item,
            sidecar_path=report.sidecar_path,
        )
        if not snapshot_ok:
            snapshot_category = (
                "sidecar_input_missing"
                if snapshot_extra.get("sidecar_input_size_bytes") is None
                or snapshot_extra.get("sidecar_input_mtime_ns") is None
                else "sidecar_input_mismatch"
            )
            return False, snapshot_category, snapshot_message, snapshot_extra
        identity_ok, identity_message, identity_extra = _validate_sidecar_manifest_relative_path(
            item=item,
            sidecar_path=report.sidecar_path,
        )
        if not identity_ok:
            return False, "sidecar_relative_path_mismatch", identity_message, identity_extra
        return True, None, None, {
            "sidecar_path": report.sidecar_path,
            "output_md_path": report.output_md_path,
        }
    category = report.failure_category or report.status or "artifact_incomplete"
    if category == "artifact_missing" and report.missing_artifacts == [report.sidecar_path]:
        category = "sidecar_missing"
    return False, category, report.error_message, {
        "sidecar_path": report.sidecar_path,
        "output_md_path": report.output_md_path,
        "sidecar_status": report.status,
        "sidecar_failure_category": report.failure_category,
        "sidecar_error_type": report.error_type,
        "missing_artifacts": report.missing_artifacts,
        "invalid_artifacts": report.invalid_artifacts,
    }


def audit_manifest_outputs(
    *,
    manifest_path: str | Path,
    output_dir: str | Path,
    check_input: bool = False,
    sample_limit: int = 20,
    max_items: int | None = None,
) -> ManifestOutputAuditReport:
    manifest = Path(manifest_path)
    output_root = Path(output_dir)
    builder = _AuditBuilder(
        manifest_path=manifest,
        output_dir=output_root,
        sample_limit=sample_limit,
    )
    seen_relative_paths: dict[str, int] = {}
    with manifest.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            if max_items is not None and builder.total_items >= max_items:
                builder.truncated = True
                break
            try:
                item = ManifestItem.from_dict(json.loads(stripped))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                builder.total_items += 1
                builder.add_issue(
                    category="invalid_manifest_row",
                    line_number=line_number,
                    message=str(exc),
                )
                continue

            builder.total_items += 1
            try:
                relative_key = _safe_relative_path(item.relative_path).as_posix()
            except ValueError as exc:
                builder.add_issue(
                    category="invalid_relative_path",
                    item=item,
                    line_number=line_number,
                    message=str(exc),
                )
                continue
            first_line_number = seen_relative_paths.get(relative_key)
            if first_line_number is not None:
                builder.add_issue(
                    category="duplicate_relative_path",
                    item=item,
                    line_number=line_number,
                    message=(
                        "duplicate relative_path maps multiple manifest rows "
                        "to the same output key"
                    ),
                    extra={"first_line_number": first_line_number},
                )
                continue
            seen_relative_paths[relative_key] = line_number
            if check_input:
                fresh, category, message = _validate_input_freshness(item)
                if not fresh:
                    builder.add_issue(
                        category=category or "input_invalid",
                        item=item,
                        line_number=line_number,
                        message=message,
                    )
                    continue

            ok, category, message, extra = _audit_item_output(
                item=item,
                output_dir=output_root,
            )
            if ok:
                builder.add_ok()
                continue
            builder.add_issue(
                category=category or "artifact_incomplete",
                item=item,
                line_number=line_number,
                message=message,
                extra=extra,
            )
    return builder.build(max_items=max_items)
