from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from .failure_category import infer_failure_category
from .resume import STATUS_SIDECAR_NAME

SECRET_MODEL_CONFIG_KEYS = {
    "api_key",
    "authorization",
    "bearer_token",
    "token",
    "access_token",
    "secret",
    "password",
    "x_api_key",
}
SECRET_MODEL_CONFIG_SUFFIXES = ("_api_key", "_token", "_secret", "_password")


def _first_output_md_path(result: list[dict[str, Any]]) -> str | None:
    for row in result or []:
        output_md_path = row.get("output_md_path")
        if output_md_path:
            return str(output_md_path)
    return None


def _artifact_paths_from_result(result: list[dict[str, Any]]) -> list[dict[str, str]]:
    artifacts: list[dict[str, str]] = []
    seen_paths: set[str] = set()

    def add_artifact(kind: str, path: Any, engine: Any = None) -> None:
        if not path:
            return
        path_value = str(path)
        if path_value in seen_paths:
            return
        seen_paths.add(path_value)
        artifact = {"kind": kind, "path": path_value}
        if engine:
            artifact["engine"] = str(engine)
        artifacts.append(artifact)

    for row in result or []:
        add_artifact("document_markdown", row.get("output_md_path"))
        add_artifact("origin_markdown", row.get("origin_md_path"))
        add_artifact("layout_pdf", row.get("layout_pdf_path"))
        add_artifact("document_json", row.get("document_json_path"))
        for native_artifact in row.get("native_artifacts") or []:
            if not isinstance(native_artifact, dict):
                continue
            add_artifact(
                str(native_artifact.get("kind") or "native_artifact"),
                native_artifact.get("path"),
                native_artifact.get("engine"),
            )
    return artifacts


def _page_status_summary(parser: Any, result: list[dict[str, Any]]) -> dict[str, Any]:
    page_results = result or []
    success_statuses = set(getattr(parser, "SUCCESS_STATUSES", {"success", "success_fallback_text", "success_fallback_image"}))
    status_counts: dict[str, int] = {}
    completed_pages = 0
    failed_pages = 0
    skipped_pages = 0
    for row in page_results:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
        if status in success_statuses:
            completed_pages += 1
        elif status == "skipped_blank":
            skipped_pages += 1
        else:
            failed_pages += 1
    return {
        "total_pages": len(page_results),
        "completed_pages": completed_pages,
        "failed_pages": failed_pages,
        "skipped_pages": skipped_pages,
        "page_status_counts": dict(sorted(status_counts.items())),
    }


def _model_config_summary(parser: Any) -> dict[str, Any]:
    fields = (
        "engine",
        "model_name",
        "ip",
        "port",
        "page_concurrency",
        "file_concurrency",
        "api_concurrency",
        "api_concurrency_start",
        "api_concurrency_max",
        "timeout",
        "max_completion_tokens",
        "engine_config",
    )
    summary: dict[str, Any] = {}
    for field in fields:
        value = getattr(parser, field, None)
        if value is not None:
            summary[field] = _redact_model_config_value(field, value)
    return summary


def _redact_model_config_value(key: str, value: Any) -> Any:
    normalized_key = key.lower().replace("-", "_")
    if normalized_key in SECRET_MODEL_CONFIG_KEYS or normalized_key.endswith(SECRET_MODEL_CONFIG_SUFFIXES):
        return "[redacted]"
    if isinstance(value, dict):
        return {
            str(child_key): _redact_model_config_value(str(child_key), child_value)
            for child_key, child_value in value.items()
        }
    if isinstance(value, list):
        return [_redact_model_config_value(key, item) for item in value]
    if isinstance(value, tuple):
        return [_redact_model_config_value(key, item) for item in value]
    return value


def _failure_category_for_sidecar(status: str, error: str | None, result: list[dict[str, Any]]) -> str | None:
    if status == "success":
        return None
    for row in result or []:
        failure_category = row.get("failure_category")
        if failure_category:
            return str(failure_category)
    return infer_failure_category({"error": error})


def _error_type_for_sidecar(
    status: str,
    result: list[dict[str, Any]],
    error_type: str | None,
) -> str | None:
    if status == "success":
        return None
    if error_type:
        return str(error_type)
    for row in result or []:
        row_error_type = row.get("error_type")
        if row_error_type:
            return str(row_error_type)
    return None


def _input_stat_summary(input_path: str) -> dict[str, int | None]:
    try:
        stat = Path(input_path).stat()
    except OSError:
        return {"input_size_bytes": None, "input_mtime_ns": None}
    return {
        "input_size_bytes": int(stat.st_size),
        "input_mtime_ns": int(stat.st_mtime_ns),
    }


def write_status_sidecar(
    *,
    parser: Any,
    save_dir: str,
    input_path: str,
    filename: str,
    status: str,
    error: str | None,
    result: list[dict[str, Any]],
    duration_seconds: float,
    error_type: str | None = None,
    manifest_input_size_bytes: int | None = None,
    manifest_input_mtime_ns: int | None = None,
    manifest_relative_path: str | None = None,
) -> None:
    sidecar_path = Path(save_dir) / STATUS_SIDECAR_NAME
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    failure_category = _failure_category_for_sidecar(status, error, result)
    payload = {
        "status": status,
        "file_path": input_path,
        "filename": filename,
        **_input_stat_summary(input_path),
        "error": error,
        "failure_category": failure_category,
        "error_type": _error_type_for_sidecar(status, result, error_type),
        "output_md_path": _first_output_md_path(result),
        "artifacts": _artifact_paths_from_result(result),
        "pages": len(result or []),
        **_page_status_summary(parser, result),
        "duration_seconds": round(max(duration_seconds, 0.0), 3),
        "model_config": _model_config_summary(parser),
    }
    if manifest_input_size_bytes is not None:
        payload["manifest_input_size_bytes"] = int(manifest_input_size_bytes)
    if manifest_input_mtime_ns is not None:
        payload["manifest_input_mtime_ns"] = int(manifest_input_mtime_ns)
    if manifest_relative_path is not None:
        payload["manifest_relative_path"] = str(manifest_relative_path)
    tmp_path = sidecar_path.with_suffix(sidecar_path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp_path, sidecar_path)
