from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any

from ocr_parser.infra.failure_category import infer_failure_category
from sqlalchemy.orm import Session

from ..schemas import ScanUnitFailRequest

TERMINAL_JOB_STATUSES = {"succeeded", "failed", "stopped"}
JOB_STATUS_FILTERS = {"queued", "running", "stopping", *TERMINAL_JOB_STATUSES}
TERMINAL_SHARD_STATUSES = {"succeeded", "failed", "stopped"}
RECLAIMABLE_SHARD_STATUSES = {"pending", "retrying", "stale"}
ATTENTION_SHARD_STATUSES = {"running", "retrying", "stale", "failed"}
CURRENT_WORKER_SHARD_STATUSES = {"running", "retrying", "stale"}
SHARD_STATUS_FILTERS = {
    "all",
    "attention",
    "pending",
    "running",
    "retrying",
    "stale",
    *TERMINAL_SHARD_STATUSES,
}
RECLAIMABLE_SCAN_UNIT_STATUSES = {"pending", "stale"}
COMPLETED_FILE_STATUSES = {"success", "succeeded", "done", "completed"}
FAILED_FILE_STATUSES = {"failed", "error"}
SKIPPED_FILE_STATUSES = {"skipped"}
PROCESSED_FILE_STATUSES = COMPLETED_FILE_STATUSES | FAILED_FILE_STATUSES | SKIPPED_FILE_STATUSES
DEGRADED_PAGE_STATUSES = {"success_fallback_image"}
PRIORITY_FAILURE_EVENT_TYPES = {"file_failed", "job_failed"}
PRIORITY_TERMINAL_EVENT_TYPES = {"file_done", "job_done", "job_stopped"}
RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED = {"manifest_scan_progress"}
RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED = 1
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
DISABLE_SAVED_MODEL_PROFILE_KEYS_ENV = "OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS"
ALLOW_SAVED_MODEL_PROFILE_KEYS_ENV = "OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS"


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY_ENV_VALUES


def saved_model_profile_keys_allowed() -> bool:
    if _env_truthy(DISABLE_SAVED_MODEL_PROFILE_KEYS_ENV):
        return False
    return _env_truthy(ALLOW_SAVED_MODEL_PROFILE_KEYS_ENV)


def _env_positive_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value > 0 else default


def _env_non_negative_int(name: str, default: int) -> int:
    raw_value = os.environ.get(name)
    if raw_value is None:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        return default
    return value if value >= 0 else default


STALE_AFTER_SECONDS = _env_positive_int("OCR_JOB_STALE_AFTER_SECONDS", 120)
SERVER_STALE_AFTER_SECONDS = _env_positive_int("OCR_SERVER_STALE_AFTER_SECONDS", 120)
SHARD_LEASE_SECONDS = _env_positive_int("OCR_SHARD_LEASE_SECONDS", 180)
JOB_FILE_DETAIL_LIMIT = _env_non_negative_int("OCR_JOB_FILE_DETAIL_LIMIT", 10000)
JOB_EVENT_DETAIL_LIMIT = _env_non_negative_int("OCR_JOB_EVENT_DETAIL_LIMIT", 50000)
JOB_LOG_DETAIL_LIMIT = _env_non_negative_int("OCR_JOB_LOG_DETAIL_LIMIT", 10000)
JOB_FAILED_FILE_SAMPLE_LIMIT = _env_non_negative_int("OCR_JOB_FAILED_FILE_SAMPLE_LIMIT", 100)
JOB_RECENT_ERROR_SAMPLE_LIMIT = _env_non_negative_int(
    "OCR_JOB_RECENT_ERROR_SAMPLE_LIMIT",
    JOB_FAILED_FILE_SAMPLE_LIMIT,
)
JOB_SUMMARY_ATTENTION_SHARD_LIMIT = _env_non_negative_int(
    "OCR_JOB_SUMMARY_ATTENTION_SHARD_LIMIT",
    50,
)
MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT = _env_non_negative_int(
    "OCR_MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT",
    50,
)
SCAN_UNIT_CLAIM_BATCH_SIZE = _env_positive_int("OCR_SCAN_UNIT_CLAIM_BATCH_SIZE", 100)
PERSIST_JOB_FILE_DETAILS = JOB_FILE_DETAIL_LIMIT != 0
PERSIST_JOB_EVENT_DETAILS = JOB_EVENT_DETAIL_LIMIT != 0
TERMINAL_EVENT_STATUSES = {
    "job_done": "succeeded",
    "job_failed": "failed",
    "job_stopped": "stopped",
}
ALLOWED_INPUT_MODES = {
    "directory",
    "folder_snapshot",
    "existing_manifest",
    "remote_folder_snapshot",
    "distributed_remote_folder_snapshot",
}
CONTROL_STATIC_INPUT_MODES = {"folder_snapshot", "existing_manifest"}
REMOTE_STATIC_INPUT_MODES = {"remote_folder_snapshot"}
REMOTE_DISTRIBUTED_SCAN_INPUT_MODES = {"distributed_remote_folder_snapshot"}
POOL_SERVER_ID = "__server_pool__"
DEFAULT_MANIFEST_ROOT_SUFFIX = ".ocr_platform/manifests"
DEFAULT_MODEL_PROFILES: dict[str, dict[str, Any]] = {
    "paddleocr_vl_local": {
        "label": "PaddleOCR-VL @ worker-1.example.internal",
        "engine": "paddleocr-vl",
        "ip": "127.0.0.1",
        "port": 30001,
        "model_name": "paddleocr-vl",
        "page_concurrency": 4,
        "extra_args": {
            "skip_blank_pages": True,
            "file_concurrency": 4,
            "api_concurrency_start": 8,
            "api_concurrency_max": 8,
            "block_concurrency": 8,
            "paddle_layout_concurrency": 2,
            "paddle_block_backpressure_high_watermark": 24,
            "paddle_block_backpressure_low_watermark": 8,
            "num_cpu_workers": 16,
            "max_retries": 1,
            "retry_delay": 1,
            "timeout": 900,
            "max_completion_tokens": 4096,
            "no_warmup": True,
            "layout_detection_url": "http://127.0.0.1:30002",
        },
        "requires_api_key": False,
        "is_default": False,
    },
    "mineru_v25": {
        "label": "MinerU 2.5 @ 127.0.0.1",
        "engine": "mineru",
        "ip": "127.0.0.1",
        "port": 30090,
        "model_name": "MinerU2.5",
        "page_concurrency": 4,
        "extra_args": {
            "skip_blank_pages": True,
            "file_concurrency": 4,
            "api_concurrency_start": 8,
            "api_concurrency_max": 8,
            "block_concurrency": 8,
            "mineru_layout_reserved_api_slots": 2,
            "mineru_recognition_api_concurrency": 6,
            "num_cpu_workers": 16,
            "max_retries": 1,
            "retry_delay": 1,
            "timeout": 900,
            "max_completion_tokens": 4096,
            "no_warmup": True,
        },
        "requires_api_key": False,
        "is_default": False,
    },
    "dotsocr_15": {
        "label": "DotsOCR 1.5 @ 127.0.0.1",
        "engine": "dotsocr",
        "ip": "127.0.0.1",
        "port": 13080,
        "model_name": "DotsOCR",
        "page_concurrency": 80,
        "extra_args": {
            "skip_blank_pages": True,
            "file_concurrency": 8,
            "api_concurrency_start": 80,
            "api_concurrency_max": 80,
            "num_cpu_workers": 56,
            "max_retries": 1,
            "retry_delay": 1,
            "timeout": 180,
            "max_completion_tokens": 4096,
            "no_warmup": True,
        },
        "requires_api_key": True,
        "is_default": True,
    },
}


class UnknownJobError(ValueError):
    pass


class JobNotTerminalError(ValueError):
    pass


class UnknownServerError(ValueError):
    pass


class ServerArchiveError(ValueError):
    pass


class ScanUnitAttemptConflictError(ValueError):
    pass


class ShardAttemptConflictError(ValueError):
    pass


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def json_dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def json_loads_object(payload: str) -> dict[str, Any]:
    if not payload:
        return {}
    value = json.loads(payload)
    return value if isinstance(value, dict) else {}


def json_loads_list(payload: str) -> list[str]:
    if not payload:
        return []
    value = json.loads(payload)
    return [str(item) for item in value] if isinstance(value, list) else []


def _scan_error_sample_with_category(item: dict[str, Any]) -> dict[str, Any]:
    sample = dict(item)
    if sample.get("failure_category"):
        sample["failure_category"] = str(sample["failure_category"])
        return sample
    sample["failure_category"] = infer_failure_category(
        {
            "error": sample.get("reason"),
            "error_message": sample.get("reason"),
            "message": sample.get("reason"),
        }
    )
    if sample["failure_category"] in {"unknown", "parser_failed"} and sample.get("reason"):
        sample["failure_category"] = "input_invalid"
    return sample


def _scan_unit_failure_category(request: ScanUnitFailRequest) -> str:
    if request.failure_category:
        return str(request.failure_category)
    category = infer_failure_category({"error_message": request.error_message})
    if category in {"unknown", "parser_failed"} and request.error_message:
        return "input_invalid"
    return category

__all__ = [name for name in globals() if not name.startswith("__")]
