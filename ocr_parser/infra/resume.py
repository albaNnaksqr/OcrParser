from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path


STATUS_SIDECAR_NAME = ".ocr_status.json"


@dataclass(frozen=True)
class ArtifactCompletenessReport:
    ok: bool
    status: str | None
    sidecar_path: str
    output_md_path: str | None = None
    artifacts: list[str] = field(default_factory=list)
    missing_artifacts: list[str] = field(default_factory=list)
    invalid_artifacts: list[dict[str, str]] = field(default_factory=list)
    failure_category: str | None = None
    error_type: str | None = None
    error_message: str | None = None


def read_status_sidecar(output_dir, filename, console_write):
    sidecar_path = Path(output_dir) / filename / STATUS_SIDECAR_NAME
    if not sidecar_path.exists():
        return None
    try:
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        console_write(f"Invalid OCR status sidecar for '{filename}': {exc}", level="warning")
        return {
            "status": "invalid",
            "path": str(sidecar_path),
            "failure_category": "sidecar_invalid",
            "error": "invalid OCR status sidecar",
        }
    if not isinstance(payload, dict):
        return {
            "status": "invalid",
            "path": str(sidecar_path),
            "failure_category": "sidecar_invalid",
            "error": "invalid OCR status sidecar",
        }
    return payload


def _expected_md_path(output_dir, filename) -> Path:
    filename_path = Path(filename)
    return Path(output_dir) / filename_path / f"{filename_path.name}.md"


def _artifact_paths_from_sidecar(sidecar, expected_md_path: Path) -> list[str]:
    paths: list[str] = []

    def add_path(value) -> None:
        if value:
            paths.append(str(value))

    add_path(sidecar.get("output_md_path") or str(expected_md_path))
    for artifact in sidecar.get("artifacts") or []:
        if isinstance(artifact, dict):
            add_path(artifact.get("path"))
        elif isinstance(artifact, str):
            add_path(artifact)

    return list(dict.fromkeys(paths))


def _artifact_is_json(path: Path) -> bool:
    return path.suffix.lower() == ".json"


def _artifact_is_jsonl(path: Path) -> bool:
    return path.suffix.lower() == ".jsonl"


def _validate_jsonl(path: Path) -> None:
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            stripped = line.strip()
            if stripped:
                json.loads(stripped)


def _artifact_filesystem_path(artifact_path: str, save_dir: Path) -> Path:
    path = Path(artifact_path)
    return path if path.is_absolute() else save_dir / path


def _artifact_is_inside_save_dir(path: Path, save_dir: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(save_dir.resolve(strict=False))
    except ValueError:
        return False
    return True


def _validate_artifact_file(path: Path, artifact_path: str) -> dict[str, str] | None:
    try:
        if not path.is_file():
            return {"path": artifact_path, "reason": "not_file"}
        if path.stat().st_size <= 0:
            return {"path": artifact_path, "reason": "empty_file"}
        if _artifact_is_json(path):
            json.loads(path.read_text(encoding="utf-8"))
        if _artifact_is_jsonl(path):
            _validate_jsonl(path)
    except json.JSONDecodeError:
        reason = "invalid_jsonl" if _artifact_is_jsonl(path) else "invalid_json"
        return {"path": artifact_path, "reason": reason}
    except OSError as exc:
        return {"path": artifact_path, "reason": str(exc)}
    return None


def _success_sidecar_page_failure(sidecar) -> str | None:
    try:
        failed_pages = int(sidecar.get("failed_pages") or 0)
    except (TypeError, ValueError):
        failed_pages = 0
    if failed_pages > 0:
        return "OCR status sidecar reports failed pages despite success status"
    page_status_counts = sidecar.get("page_status_counts") or {}
    if isinstance(page_status_counts, dict):
        try:
            error_pages = int(page_status_counts.get("error") or 0)
        except (TypeError, ValueError):
            error_pages = 0
        if error_pages > 0:
            return "OCR status sidecar reports failed pages despite success status"
    return None


def check_output_artifacts(output_dir, filename, console_write) -> ArtifactCompletenessReport:
    sidecar_path = Path(output_dir) / filename / STATUS_SIDECAR_NAME
    expected_md_path = _expected_md_path(output_dir, filename)
    save_dir = Path(output_dir) / filename
    sidecar = read_status_sidecar(output_dir, filename, console_write)
    if sidecar is None:
        return ArtifactCompletenessReport(
            ok=False,
            status=None,
            sidecar_path=str(sidecar_path),
            output_md_path=str(expected_md_path),
            missing_artifacts=[str(sidecar_path)],
            failure_category="artifact_missing",
            error_message="missing OCR status sidecar",
        )

    status = sidecar.get("status")
    output_md_path = str(sidecar.get("output_md_path") or expected_md_path)
    if status != "success":
        sidecar_failure_category = sidecar.get("failure_category") or "artifact_incomplete"
        sidecar_error_type = sidecar.get("error_type")
        sidecar_error = sidecar.get("error")
        invalid_artifacts = []
        if sidecar_failure_category == "sidecar_invalid":
            invalid_artifacts.append({"path": str(sidecar_path), "reason": "invalid_sidecar"})
        return ArtifactCompletenessReport(
            ok=False,
            status=str(status) if status is not None else None,
            sidecar_path=str(sidecar_path),
            output_md_path=output_md_path,
            failure_category=str(sidecar_failure_category),
            error_type=str(sidecar_error_type) if sidecar_error_type else None,
            error_message=str(sidecar_error) if sidecar_error else f"OCR status sidecar is not successful: {status}",
            invalid_artifacts=invalid_artifacts,
        )

    page_failure = _success_sidecar_page_failure(sidecar)
    if page_failure is not None:
        return ArtifactCompletenessReport(
            ok=False,
            status=str(status),
            sidecar_path=str(sidecar_path),
            output_md_path=output_md_path,
            failure_category="page_failure",
            error_message=page_failure,
        )

    artifact_paths = _artifact_paths_from_sidecar(sidecar, expected_md_path)
    missing_artifacts: list[str] = []
    invalid_artifacts: list[dict[str, str]] = []
    for artifact_path in artifact_paths:
        path = _artifact_filesystem_path(artifact_path, save_dir)
        if not _artifact_is_inside_save_dir(path, save_dir):
            invalid_artifacts.append({"path": artifact_path, "reason": "outside_output_dir"})
            continue
        if not path.exists():
            missing_artifacts.append(artifact_path)
            continue
        invalid_artifact = _validate_artifact_file(path, artifact_path)
        if invalid_artifact is not None:
            invalid_artifacts.append(invalid_artifact)

    failure_category = None
    error_message = None
    if missing_artifacts:
        failure_category = "artifact_missing"
        error_message = "one or more declared OCR artifacts are missing"
    elif invalid_artifacts:
        failure_category = "artifact_invalid"
        error_message = "one or more declared OCR artifacts are invalid"

    return ArtifactCompletenessReport(
        ok=not missing_artifacts and not invalid_artifacts,
        status=str(status),
        sidecar_path=str(sidecar_path),
        output_md_path=output_md_path,
        artifacts=artifact_paths,
        missing_artifacts=missing_artifacts,
        invalid_artifacts=invalid_artifacts,
        failure_category=failure_category,
        error_message=error_message,
    )


def _sidecar_input_snapshot_matches(
    sidecar,
    *,
    expected_input_size_bytes: int | None,
    expected_input_mtime_ns: int | None,
    require_input_snapshot: bool = False,
) -> tuple[bool, str | None]:
    sidecar_size = sidecar.get("input_size_bytes")
    sidecar_mtime = sidecar.get("input_mtime_ns")
    if require_input_snapshot and (sidecar_size is None or sidecar_mtime is None):
        return False, "OCR status sidecar is missing input_size_bytes or input_mtime_ns"
    if sidecar_size is None and sidecar_mtime is None:
        return True, None
    try:
        if (
            expected_input_size_bytes is not None
            and sidecar_size is not None
            and int(sidecar_size) != int(expected_input_size_bytes)
        ):
            return False, (
                "OCR status sidecar input_size_bytes does not match manifest row "
                f"({sidecar_size}!={expected_input_size_bytes})"
            )
        if (
            expected_input_mtime_ns is not None
            and sidecar_mtime is not None
            and int(sidecar_mtime) != int(expected_input_mtime_ns)
        ):
            return False, (
                "OCR status sidecar input_mtime_ns does not match manifest row "
                f"({sidecar_mtime}!={expected_input_mtime_ns})"
            )
    except (TypeError, ValueError):
        return False, "OCR status sidecar input snapshot is invalid"
    return True, None


def _sidecar_manifest_relative_path_matches(
    sidecar,
    *,
    expected_manifest_relative_path: str | None,
) -> tuple[bool, str | None]:
    if expected_manifest_relative_path is None:
        return True, None
    sidecar_relative_path = sidecar.get("manifest_relative_path")
    if sidecar_relative_path is None:
        return True, None
    if str(sidecar_relative_path) == str(expected_manifest_relative_path):
        return True, None
    return False, (
        "OCR status sidecar manifest_relative_path does not match manifest row "
        f"({sidecar_relative_path}!={expected_manifest_relative_path})"
    )


def is_file_already_processed(
    input_path,
    output_dir,
    filename,
    console_write,
    *,
    require_status_sidecar: bool = False,
    expected_input_size_bytes: int | None = None,
    expected_input_mtime_ns: int | None = None,
    require_input_snapshot: bool = False,
    expected_manifest_relative_path: str | None = None,
):
    try:
        save_dir = Path(output_dir) / filename
        expected_md_path = _expected_md_path(output_dir, filename)
        sidecar = read_status_sidecar(output_dir, filename, console_write)
        if sidecar is not None:
            snapshot_matches, snapshot_error = _sidecar_input_snapshot_matches(
                sidecar,
                expected_input_size_bytes=expected_input_size_bytes,
                expected_input_mtime_ns=expected_input_mtime_ns,
                require_input_snapshot=require_input_snapshot,
            )
            if not snapshot_matches:
                console_write(
                    f"OCR status sidecar for '{filename}' does not match the manifest input snapshot "
                    f"({snapshot_error}); will reprocess.",
                    level="warning",
                )
                return False, str(sidecar.get("output_md_path") or expected_md_path)
            identity_matches, identity_error = _sidecar_manifest_relative_path_matches(
                sidecar,
                expected_manifest_relative_path=expected_manifest_relative_path,
            )
            if not identity_matches:
                console_write(
                    f"OCR status sidecar for '{filename}' does not match the manifest output identity "
                    f"({identity_error}); will reprocess.",
                    level="warning",
                )
                return False, str(sidecar.get("output_md_path") or expected_md_path)
            report = check_output_artifacts(output_dir, filename, console_write)
            if report.ok:
                return True, report.output_md_path
            console_write(
                f"OCR output artifacts for '{filename}' are incomplete "
                f"({report.failure_category or report.status}); will reprocess.",
                level="warning",
            )
            return False, report.output_md_path

        candidate_md_paths = [expected_md_path]
        legacy_md_path = Path(output_dir) / f"{filename}.md"
        if legacy_md_path != expected_md_path:
            candidate_md_paths.append(legacy_md_path)
        if not expected_md_path.exists() and save_dir.is_dir():
            for md_file in save_dir.glob("*.md"):
                if md_file not in candidate_md_paths:
                    candidate_md_paths.append(md_file)

        if require_status_sidecar:
            for md_path in candidate_md_paths:
                if md_path.exists():
                    console_write(
                        f"Found legacy MD output for '{filename}' without OCR status sidecar; will reprocess.",
                        level="warning",
                    )
                    return False, str(md_path)
            return False, None

        for md_path in candidate_md_paths:
            if md_path.exists():
                md_size = md_path.stat().st_size
                if md_size > 0:
                    if md_path != expected_md_path:
                        console_write(
                            f"Detected existing MD output for '{filename}' at unexpected path: {md_path}.",
                            level="warning",
                        )
                    return True, str(md_path)
                console_write(f"Found empty MD file: {md_path}, will reprocess", level="warning")
                return False, str(md_path)
        return False, None
    except Exception as exc:
        console_write(f"Error checking if file is processed: {exc}", level="error")
        return False, None


def cleanup_incomplete_output_dir(output_dir, filename, console_write):
    try:
        save_dir = Path(output_dir) / filename
        if not save_dir.is_dir():
            return
        console_write(f"Found incomplete output for '{filename}'. Cleaning directory: {save_dir}", level="warning")
        shutil.rmtree(save_dir)
    except OSError as exc:
        console_write(f"Warning: Could not clean up directory {save_dir}. Error: {exc}", level="warning")
    except Exception as exc:
        console_write(f"Unexpected error cleaning directory {save_dir}: {exc}", level="error")
