from __future__ import annotations

import json
import math
import os
import posixpath
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ocr_parser.infra.failure_category import infer_failure_category
from ocr_parser.config import ParserConfig
from ocr_platform.manifest.models import ManifestItem
from ocr_platform.manifest.scanner import scan_folder_snapshot
from ocr_platform.manifest.sharder import write_manifest_snapshot
from sqlalchemy import Integer, case, delete, distinct, func, select, update
from sqlalchemy.orm import Session

from . import database
from .models import Job, JobCounter, JobEvent, JobFile, JobLog, Manifest, ModelProfile, ScanUnit, Server, ShardAttempt, WorkShard
from .schemas import (
    JobCreateRequest,
    JobEventRequest,
    JobLogListResponse,
    JobLogRequest,
    JobLogResponse,
    ManifestFreezeReportResponse,
    ManifestIntegrityResponse,
    ManifestIntegrityWorkerCompleteRequest,
    ManifestIntegrityWorkerRequestResponse,
    ManifestIntegrityWorkerTask,
    ManifestIntegrityWorkerShardTask,
    ManifestIntegrityScanUnitIssue,
    ManifestIntegrityShardIssue,
    JobPreflightIssue,
    JobPreflightResponse,
    JobRecentErrorListResponse,
    JobRecentErrorResponse,
    JobSummaryListResponse,
    JobShardProgressSummary,
    JobSummaryResponse,
    JobWorkerShardSummary,
    ModelProfileRequest,
    ModelProfileResponse,
    ScanUnitCompleteRequest,
    ScanUnitFailRequest,
    ServerHeartbeatRequest,
    ServerRegisterRequest,
    ShardAttemptListResponse,
    WorkShardUpdateRequest,
    RemoteManifestRegisterRequest,
    ShardAttemptResponse,
)


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


def ensure_default_model_profiles(session: Session) -> None:
    changed = False
    for profile_id, defaults in DEFAULT_MODEL_PROFILES.items():
        if session.get(ModelProfile, profile_id) is not None:
            continue
        session.add(
            ModelProfile(
                id=profile_id,
                label=str(defaults["label"]),
                engine=str(defaults["engine"]),
                ip=defaults.get("ip"),
                port=defaults.get("port"),
                model_name=defaults.get("model_name"),
                page_concurrency=defaults.get("page_concurrency"),
                extra_args_json=json_dumps(defaults.get("extra_args", {})),
                requires_api_key=bool(defaults.get("requires_api_key", False)),
                is_default=bool(defaults.get("is_default", False)),
            )
        )
        changed = True
    if changed:
        session.commit()


def model_profile_to_response(profile: ModelProfile) -> ModelProfileResponse:
    return ModelProfileResponse(
        id=profile.id,
        label=profile.label,
        engine=profile.engine,
        ip=profile.ip,
        port=profile.port,
        model_name=profile.model_name,
        page_concurrency=profile.page_concurrency,
        extra_args=json_loads_object(profile.extra_args_json),
        requires_api_key=profile.requires_api_key,
        has_api_key=bool(_resolve_model_profile_api_key(profile)),
        api_key_env_var=profile.api_key_env_var,
        is_default=profile.is_default,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def list_model_profiles(session: Session) -> list[ModelProfile]:
    ensure_default_model_profiles(session)
    return session.execute(select(ModelProfile).order_by(ModelProfile.id)).scalars().all()


def get_model_profile_or_raise(session: Session, profile_id: str) -> ModelProfile:
    ensure_default_model_profiles(session)
    profile = session.get(ModelProfile, profile_id)
    if profile is None:
        raise ValueError(f"unknown model_profile_id: {profile_id}")
    return profile


def _is_secret_like_extra_arg_name(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    if normalized in {"api_key", "api_key_env_var", "authorization", "password"}:
        return True
    return normalized.endswith(("_token", "_secret", "_password"))


def _reject_secret_like_extra_args(
    extra_args: dict[str, Any],
    *,
    context: str,
    allowed_names: set[str] | None = None,
) -> None:
    allowed = allowed_names or set()
    rejected = sorted(
        name
        for name in extra_args
        if name not in allowed and _is_secret_like_extra_arg_name(str(name))
    )
    if not rejected:
        return
    joined = ", ".join(rejected)
    raise ValueError(
        f"{context} extra_args may not contain secret-like keys: {joined}; "
        "use api_key/api_key_env_var dedicated fields instead"
    )


def _normalize_parser_extra_args(extra_args: dict[str, Any], *, context: str) -> dict[str, Any]:
    return ParserConfig.validate_option_dict(extra_args or {}, context=f"{context} extra_args")


def upsert_model_profile(session: Session, profile_id: str, request: ModelProfileRequest) -> ModelProfile:
    ensure_default_model_profiles(session)
    _reject_secret_like_extra_args(request.extra_args, context="model profile")
    normalized_extra_args = _normalize_parser_extra_args(request.extra_args, context="model profile")
    profile = session.get(ModelProfile, profile_id)
    if profile is None:
        profile = ModelProfile(id=profile_id)
        session.add(profile)

    if request.is_default:
        session.execute(update(ModelProfile).where(ModelProfile.id != profile_id).values(is_default=False))

    saved_profile_keys_disabled = not saved_model_profile_keys_allowed()
    requested_saved_key = request.api_key is not None and bool(request.api_key)
    existing_saved_key_would_remain = bool(profile.api_key) and not request.clear_api_key and request.api_key is None
    if saved_profile_keys_disabled and requested_saved_key:
        raise ValueError(
            "saved model profile api_key is disabled; set api_key_env_var on the control server environment instead"
        )
    if saved_profile_keys_disabled and existing_saved_key_would_remain:
        raise ValueError(
            "saved model profile api_key is disabled; set clear_api_key=true and use api_key_env_var instead"
        )

    profile.label = request.label
    profile.engine = request.engine
    profile.ip = request.ip
    profile.port = request.port
    profile.model_name = request.model_name
    profile.page_concurrency = request.page_concurrency
    profile.extra_args_json = json_dumps(normalized_extra_args)
    profile.requires_api_key = request.requires_api_key
    profile.is_default = request.is_default
    profile.api_key_env_var = (request.api_key_env_var or "").strip() or None
    if request.clear_api_key:
        profile.api_key = None
    elif request.api_key is not None:
        profile.api_key = request.api_key or None

    session.commit()
    session.refresh(profile)
    return profile


def _resolve_model_profile_api_key(profile: ModelProfile) -> str | None:
    if profile.api_key:
        return profile.api_key
    if profile.api_key_env_var:
        value = os.environ.get(profile.api_key_env_var)
        return value or None
    return None


def _resolve_job_extra_args_api_key_env_var(extra_args: dict[str, Any]) -> str | None:
    raw_env_var = extra_args.get("api_key_env_var")
    if raw_env_var is None:
        return None
    env_var = str(raw_env_var).strip()
    if not env_var:
        return None
    value = os.environ.get(env_var)
    if not value:
        raise ValueError(
            f"job extra_args api_key_env_var is not set in the control server environment: {env_var}"
        )
    return value


def _validate_job_extra_args_saved_api_key(extra_args: dict[str, Any]) -> None:
    if saved_model_profile_keys_allowed():
        return
    if extra_args.get("api_key"):
        raise ValueError(
            "saved job api_key is disabled; set extra_args.api_key_env_var on the control server environment instead"
        )


def _effective_job_model_config(session: Session, request: JobCreateRequest) -> dict[str, Any]:
    _reject_secret_like_extra_args(
        request.extra_args,
        context="job",
        allowed_names={"api_key", "api_key_env_var"},
    )
    request_extra_args = _normalize_parser_extra_args(request.extra_args, context="job")
    _validate_job_extra_args_saved_api_key(request_extra_args)
    if not request.model_profile_id:
        _resolve_job_extra_args_api_key_env_var(request_extra_args)
        return {
            "engine": request.engine,
            "ip": request.ip,
            "port": request.port,
            "model_name": request.model_name,
            "page_concurrency": request.page_concurrency,
            "extra_args": dict(request_extra_args),
        }

    profile = get_model_profile_or_raise(session, request.model_profile_id)
    profile_extra_args = _normalize_parser_extra_args(
        json_loads_object(profile.extra_args_json),
        context="model profile",
    )
    extra_args = _normalize_parser_extra_args(
        {**profile_extra_args, **request_extra_args},
        context="job",
    )
    job_api_key_from_env = _resolve_job_extra_args_api_key_env_var(extra_args)
    has_api_key = bool(
        extra_args.get("api_key")
        or job_api_key_from_env
        or _resolve_model_profile_api_key(profile)
    )
    if profile.requires_api_key and not has_api_key:
        raise ValueError(f"model profile requires api_key: {request.model_profile_id}")

    return {
        "engine": request.engine or profile.engine,
        "ip": request.ip if request.ip is not None else profile.ip,
        "port": request.port if request.port is not None else profile.port,
        "model_name": request.model_name if request.model_name is not None else profile.model_name,
        "page_concurrency": (
            request.page_concurrency if request.page_concurrency is not None else profile.page_concurrency
        ),
        "extra_args": {key: value for key, value in extra_args.items() if key != "api_key"},
    }


def allowed_server_ids_for_job(job: Job) -> list[str]:
    return json_loads_list(job.allowed_server_ids_json)


def server_is_allowed_for_job(job: Job, server_id: str) -> bool:
    allowed_server_ids = allowed_server_ids_for_job(job)
    return not allowed_server_ids or server_id in allowed_server_ids


def register_server(session: Session, request: ServerRegisterRequest) -> Server:
    server = session.get(Server, request.id)
    if server is None:
        server = Server(id=request.id, name=request.name, host=request.host)
        session.add(server)

    server.name = request.name
    server.host = request.host
    server.capacity_slots = request.capacity_slots
    server.capabilities_json = json_dumps(request.capabilities)
    server.status = "online"
    server.last_heartbeat_at = utcnow()
    server.archived_at = None
    session.commit()
    session.refresh(server)
    return server


def ensure_pool_server(session: Session) -> Server:
    server = session.get(Server, POOL_SERVER_ID)
    if server is None:
        server = Server(
            id=POOL_SERVER_ID,
            name="Server Pool",
            host="pool",
            status="online",
            capacity_slots=0,
            capabilities_json=json_dumps({"pool": True}),
            archived_at=None,
        )
        session.add(server)
        session.flush()
    elif server.archived_at is not None:
        server.archived_at = None
    return server


def public_assigned_server_id(job: Job) -> str | None:
    return None if job.assigned_server_id == POOL_SERVER_ID else job.assigned_server_id


def heartbeat_server(session: Session, server_id: str, request: ServerHeartbeatRequest) -> Server:
    server = session.get(Server, server_id)
    if server is None:
        server = Server(id=server_id, name=server_id, host=server_id)
        session.add(server)

    existing_capabilities = json_loads_object(server.capabilities_json)
    merged_capabilities = {**existing_capabilities, **request.capabilities}
    server.status = request.status
    server.capabilities_json = json_dumps(merged_capabilities)
    server.last_heartbeat_at = utcnow()
    server.archived_at = None
    renew_running_shard_leases(session, server_id)
    renew_running_scan_unit_leases(session, server_id)
    session.commit()
    session.refresh(server)
    return server


def shard_lease_deadline(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=SHARD_LEASE_SECONDS)


def scan_unit_lease_deadline(now: datetime | None = None) -> datetime:
    return (now or utcnow()) + timedelta(seconds=SHARD_LEASE_SECONDS)


def _expired_running_shard_filter(now: datetime):
    return (
        (WorkShard.status == "running")
        & (WorkShard.lease_expires_at.is_not(None))
        & (WorkShard.lease_expires_at <= now)
    )


def reconcile_expired_shard_leases(session: Session, *, now: datetime | None = None, job_id: str | None = None) -> None:
    current_time = now or utcnow()
    expired_shard_probe = (
        select(WorkShard.id)
        .where(_expired_running_shard_filter(current_time))
        .limit(1)
    )
    if job_id is not None:
        expired_shard_probe = expired_shard_probe.where(WorkShard.job_id == job_id)
    if session.execute(expired_shard_probe).scalar_one_or_none() is None:
        return

    stopped_parent = (
        select(Job.id)
        .where(Job.id == WorkShard.job_id)
        .where(
            (Job.stop_requested.is_(True))
            | (Job.status == "stopping")
        )
        .exists()
    )
    current_shard_attempt_number = (
        select(WorkShard.attempt_count)
        .where(WorkShard.id == ShardAttempt.shard_id)
        .scalar_subquery()
    )
    stopped_shard_ids = (
        select(WorkShard.id)
        .join(Job, Job.id == WorkShard.job_id)
        .where(_expired_running_shard_filter(current_time))
        .where((Job.stop_requested.is_(True)) | (Job.status == "stopping"))
    )
    if job_id is not None:
        stopped_shard_ids = stopped_shard_ids.where(WorkShard.job_id == job_id)
    session.execute(
        update(ShardAttempt)
        .where(ShardAttempt.shard_id.in_(stopped_shard_ids))
        .where(ShardAttempt.attempt_number == current_shard_attempt_number)
        .where(ShardAttempt.status == "running")
        .values(
            status="stopped",
            failure_category="operator_stopped",
            finished_at=current_time,
        )
    )
    stop_stmt = (
        update(WorkShard)
        .where(_expired_running_shard_filter(current_time))
        .where(stopped_parent)
        .values(
            status="stopped",
            failure_category="operator_stopped",
            lease_expires_at=None,
            finished_at=current_time,
        )
    )
    if job_id is not None:
        stop_stmt = stop_stmt.where(WorkShard.job_id == job_id)
    session.execute(stop_stmt)

    stale_shard_ids = (
        select(WorkShard.id)
        .where(_expired_running_shard_filter(current_time))
    )
    if job_id is not None:
        stale_shard_ids = stale_shard_ids.where(WorkShard.job_id == job_id)
    session.execute(
        update(ShardAttempt)
        .where(ShardAttempt.shard_id.in_(stale_shard_ids))
        .where(ShardAttempt.attempt_number == current_shard_attempt_number)
        .where(ShardAttempt.status == "running")
        .values(
            status="stale",
            failure_category="lease_expired",
            finished_at=current_time,
        )
    )
    stale_stmt = (
        update(WorkShard)
        .where(_expired_running_shard_filter(current_time))
        .values(status="stale", lease_expires_at=None)
    )
    if job_id is not None:
        stale_stmt = stale_stmt.where(WorkShard.job_id == job_id)
    session.execute(stale_stmt)


def reconcile_expired_scan_unit_leases(session: Session, *, now: datetime | None = None, job_id: str | None = None) -> None:
    current_time = now or utcnow()
    stmt = (
        update(ScanUnit)
        .where(ScanUnit.status == "running")
        .where(ScanUnit.lease_expires_at.is_not(None))
        .where(ScanUnit.lease_expires_at <= current_time)
        .values(
            status="stale",
            lease_expires_at=None,
            failure_category="lease_expired",
            error_message="scan unit lease expired",
        )
    )
    if job_id is not None:
        stmt = stmt.where(ScanUnit.job_id == job_id)
    session.execute(stmt)


def _remaining_retry_status(job: Job, shard: WorkShard) -> str:
    return "retrying" if shard.attempt_count < job.max_shard_attempts else "failed"


def renew_running_shard_leases(session: Session, server_id: str) -> None:
    session.execute(
        update(WorkShard)
        .where(WorkShard.assigned_server_id == server_id)
        .where(WorkShard.status == "running")
        .values(lease_expires_at=shard_lease_deadline())
    )


def renew_running_scan_unit_leases(session: Session, server_id: str) -> None:
    session.execute(
        update(ScanUnit)
        .where(ScanUnit.assigned_server_id == server_id)
        .where(ScanUnit.status == "running")
        .values(lease_expires_at=scan_unit_lease_deadline())
    )


def is_server_stale(server: Server, now: datetime | None = None) -> bool:
    if server.last_heartbeat_at is None:
        return False
    return (now or utcnow()) - server.last_heartbeat_at > timedelta(seconds=SERVER_STALE_AFTER_SECONDS)


def effective_server_status(server: Server, now: datetime | None = None) -> str:
    if is_server_stale(server, now):
        return "offline"
    return server.status


def count_active_jobs_for_server(session: Session, server_id: str) -> int:
    return int(
        session.execute(
            select(func.count(Job.id))
            .where(Job.assigned_server_id == server_id)
            .where(Job.status.in_({"running", "stopping"}))
        ).scalar_one()
        or 0
    )


def count_open_jobs_for_server(session: Session, server_id: str) -> int:
    return int(
        session.execute(
            select(func.count(Job.id))
            .where(Job.assigned_server_id == server_id)
            .where(Job.status.not_in(TERMINAL_JOB_STATUSES))
        ).scalar_one()
        or 0
    )


def stop_assigned_queued_jobs_for_server(session: Session, server_id: str) -> None:
    current_time = utcnow()
    jobs = list(
        session.execute(
            select(Job)
            .where(Job.assigned_server_id == server_id)
            .where(Job.status == "queued")
        ).scalars()
    )
    for job in jobs:
        job.stop_requested = True
        job.status = "stopped"
        if job.failure_category is None:
            job.failure_category = "operator_stopped"
        if job.finished_at is None:
            job.finished_at = current_time
        stop_reclaimable_work_for_job(session, job)
    if jobs:
        session.flush()


def count_running_shards_for_server(session: Session, server_id: str) -> int:
    return int(
        session.execute(
            select(func.count(WorkShard.id))
            .where(WorkShard.assigned_server_id == server_id)
            .where(WorkShard.status == "running")
        ).scalar_one()
        or 0
    )


def _normal_posix_path(path: str) -> str:
    normalized = posixpath.normpath(path)
    return normalized if normalized.startswith("/") else f"/{normalized}"


def _path_is_under(root: str, candidate: str) -> bool:
    normalized_root = _normal_posix_path(root).rstrip("/")
    normalized_candidate = _normal_posix_path(candidate)
    if not normalized_root:
        normalized_root = "/"
    return normalized_candidate == normalized_root or normalized_candidate.startswith(
        normalized_root + "/"
    )


def evaluate_server_path_access(
    server: Server,
    input_dir: str,
    *,
    require_writable: bool = False,
) -> dict[str, Any]:
    if server.archived_at is not None:
        return {
            "server_id": server.id,
            "name": server.name,
            "host": server.host,
            "status": "archived",
            "is_stale": True,
            "can_access": False,
            "matched_path": None,
            "reason": "server_archived",
        }

    status = effective_server_status(server)
    stale = is_server_stale(server)
    if status == "offline" or stale:
        return {
            "server_id": server.id,
            "name": server.name,
            "host": server.host,
            "status": status,
            "is_stale": stale,
            "can_access": False,
            "matched_path": None,
            "reason": "server_offline",
        }

    capabilities = json_loads_object(server.capabilities_json)
    checks = capabilities.get("shared_paths") or []
    if not isinstance(checks, list) or not checks:
        return {
            "server_id": server.id,
            "name": server.name,
            "host": server.host,
            "status": status,
            "is_stale": stale,
            "can_access": False,
            "matched_path": None,
            "reason": "no_path_checks",
        }

    matched_unavailable = None
    for check in checks:
        if not isinstance(check, dict) or not check.get("path"):
            continue
        path = str(check["path"])
        if not _path_is_under(path, input_dir):
            continue
        has_required_access = (
            check.get("exists")
            and check.get("is_dir")
            and check.get("readable")
            and (not require_writable or check.get("writable"))
        )
        if has_required_access:
            return {
                "server_id": server.id,
                "name": server.name,
                "host": server.host,
                "status": status,
                "is_stale": stale,
                "can_access": True,
                "matched_path": path,
                "reason": "ok",
            }
        matched_unavailable = path

    reason = "shared_root_unavailable" if matched_unavailable else "no_matching_shared_root"
    if matched_unavailable and require_writable:
        reason = "shared_root_not_writable"
    return {
        "server_id": server.id,
        "name": server.name,
        "host": server.host,
        "status": status,
        "is_stale": stale,
        "can_access": False,
        "matched_path": matched_unavailable,
        "reason": reason,
    }


def list_server_eligibility(session: Session, input_dir: str) -> list[dict[str, Any]]:
    return [
        evaluate_server_path_access(server, input_dir)
        for server in list_servers(session)
    ]


def _preflight_issue(
    severity: str,
    code: str,
    message: str,
    **details: Any,
) -> JobPreflightIssue:
    return JobPreflightIssue(
        severity=severity,
        code=code,
        message=message,
        details=details,
    )


def _database_migration_preflight_issue(database_status: dict[str, Any]) -> JobPreflightIssue | None:
    dialect = str(database_status.get("dialect") or "")
    if dialect != "postgresql":
        return None

    known_migrations = [str(item) for item in database_status.get("known_migrations") or []]
    latest_known_migration = known_migrations[-1] if known_migrations else None
    if not database_status.get("schema_migrations_table_exists"):
        return _preflight_issue(
            "error",
            "database_migrations_missing",
            "PostgreSQL control database is missing schema_migrations; apply the SQL migration baseline before creating production jobs.",
            dialect=dialect,
            known_migrations=known_migrations,
            latest_known_migration=latest_known_migration,
        )

    applied_versions: set[str] = set()
    for item in database_status.get("applied_migrations") or []:
        if isinstance(item, dict) and item.get("version"):
            applied_versions.add(str(item["version"]))
        elif item:
            applied_versions.add(str(item))
    missing_migrations = [version for version in known_migrations if version not in applied_versions]
    if missing_migrations:
        return _preflight_issue(
            "error",
            "database_migration_not_current",
            "PostgreSQL control database has unapplied SQL migrations; apply migrations before creating production jobs.",
            dialect=dialect,
            known_migrations=known_migrations,
            missing_migrations=missing_migrations,
            latest_known_migration=latest_known_migration,
            latest_applied_migration=database_status.get("latest_applied_migration"),
        )

    return None


def _control_api_auth_preflight_issue() -> JobPreflightIssue | None:
    if os.environ.get("OCR_PLATFORM_API_TOKEN"):
        return None
    return _preflight_issue(
        "warning",
        "control_api_auth_disabled",
        "Control API token authentication is not configured; set OCR_PLATFORM_API_TOKEN before exposing production endpoints.",
        require_api_token=_env_truthy("OCR_PLATFORM_REQUIRE_API_TOKEN"),
    )


def _server_versions(session: Session, server_ids: set[str]) -> dict[str, list[str]]:
    versions: dict[str, list[str]] = {}
    if not server_ids:
        return versions
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
    ).scalars().all()
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        key = " / ".join(
            [
                str(capabilities.get("git_ref") or "unknown git"),
                str(capabilities.get("script_version") or "unknown script"),
            ]
        )
        versions.setdefault(key, []).append(server.id)
    return versions


def _job_worker_server_ids(session: Session, job: Job) -> set[str]:
    server_ids = {
        str(server_id)
        for server_id in allowed_server_ids_for_job(job)
        if server_id and server_id != POOL_SERVER_ID
    }
    if job.assigned_server_id and job.assigned_server_id != POOL_SERVER_ID:
        server_ids.add(job.assigned_server_id)
    assigned_shard_servers = session.execute(
        select(WorkShard.assigned_server_id)
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.assigned_server_id.is_not(None))
    ).scalars().all()
    server_ids.update(str(server_id) for server_id in assigned_shard_servers if server_id)
    return server_ids


def _job_worker_version_summary(session: Session, job: Job) -> dict[str, Any]:
    versions = _server_versions(session, _job_worker_server_ids(session, job))
    if not versions:
        return {
            "worker_version_status": "unknown",
            "worker_version_warning": None,
            "worker_version_refs": {},
        }
    if len(versions) == 1:
        return {
            "worker_version_status": "consistent",
            "worker_version_warning": None,
            "worker_version_refs": versions,
        }
    return {
        "worker_version_status": "mixed",
        "worker_version_warning": "assigned workers report different git_ref or script_version values",
        "worker_version_refs": versions,
    }


def _resource_constrained_workers(session: Session, server_ids: set[str]) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    constrained: list[dict[str, Any]] = []
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        pressure = capabilities.get("resource_pressure")
        if not isinstance(pressure, dict) or not pressure.get("constrained"):
            continue
        reasons = pressure.get("reasons")
        constrained.append(
            {
                "server_id": server.id,
                "level": str(pressure.get("level") or "constrained"),
                "reasons": [str(item) for item in reasons] if isinstance(reasons, list) else [],
            }
        )
    return constrained


def _nonnegative_int(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return 0
    return max(number, 0)


def _workers_with_event_spool_backlog(session: Session, server_ids: set[str]) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    workers: list[dict[str, Any]] = []
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        spool = capabilities.get("event_spool")
        if not isinstance(spool, dict):
            continue
        pending_events = _nonnegative_int(spool.get("pending_events"))
        pending_logs = _nonnegative_int(spool.get("pending_logs"))
        failed_events = _nonnegative_int(spool.get("failed_events"))
        failed_logs = _nonnegative_int(spool.get("failed_logs"))
        dropped_events = _nonnegative_int(spool.get("dropped_events"))
        dropped_logs = _nonnegative_int(spool.get("dropped_logs"))
        total_backlog = (
            pending_events
            + pending_logs
            + failed_events
            + failed_logs
            + dropped_events
            + dropped_logs
        )
        if total_backlog <= 0:
            continue
        workers.append(
            {
                "server_id": server.id,
                "dir": str(spool.get("dir") or ""),
                "pending_events": pending_events,
                "pending_logs": pending_logs,
                "failed_events": failed_events,
                "failed_logs": failed_logs,
                "dropped_events": dropped_events,
                "dropped_logs": dropped_logs,
                "total_backlog": total_backlog,
            }
        )
    return workers


def _workers_with_pending_shard_update_backlog(session: Session, server_ids: set[str]) -> list[dict[str, Any]]:
    if not server_ids:
        return []
    servers = session.execute(
        select(Server)
        .where(Server.id.in_(server_ids))
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    workers: list[dict[str, Any]] = []
    for server in servers:
        capabilities = json_loads_object(server.capabilities_json)
        pending_updates = capabilities.get("pending_shard_updates")
        if not isinstance(pending_updates, dict):
            continue
        pending = _nonnegative_int(pending_updates.get("pending"))
        failed = _nonnegative_int(pending_updates.get("failed"))
        total_backlog = pending + failed
        if total_backlog <= 0:
            continue
        workers.append(
            {
                "server_id": server.id,
                "pending": pending,
                "failed": failed,
                "total_backlog": total_backlog,
            }
        )
    return workers


def preflight_job(session: Session, request: JobCreateRequest) -> JobPreflightResponse:
    ensure_pool_server(session)
    ensure_default_model_profiles(session)
    issues: list[JobPreflightIssue] = []
    database_status = database.describe_database_status(session.get_bind())
    database_dialect = str(database_status.get("dialect") or session.get_bind().dialect.name)
    if database_dialect != "postgresql":
        issues.append(
            _preflight_issue(
                "warning",
                "database_not_postgres",
                "Production jobs should use PostgreSQL; SQLite is for local development.",
                dialect=database_dialect,
                require_postgres=_env_truthy("OCR_PLATFORM_REQUIRE_POSTGRES"),
            )
        )
    migration_issue = _database_migration_preflight_issue(database_status)
    if migration_issue is not None:
        issues.append(migration_issue)
    auth_issue = _control_api_auth_preflight_issue()
    if auth_issue is not None:
        issues.append(auth_issue)

    allowed_ids = set(request.allowed_server_ids or [])
    if request.assigned_server_id:
        allowed_ids.add(request.assigned_server_id)
    eligibilities = [
        item
        for item in list_server_eligibility(session, request.input_dir)
        if item["server_id"] != POOL_SERVER_ID
        and (not allowed_ids or item["server_id"] in allowed_ids)
    ]
    eligible = [item for item in eligibilities if item.get("can_access")]
    ready = [item for item in eligible if item.get("status") in {"online", "idle"} and not item.get("is_stale")]
    if not eligible:
        issues.append(
            _preflight_issue(
                "error",
                "no_eligible_workers",
                "No selected worker can read the input shared path.",
                input_dir=request.input_dir,
            )
        )
    eligible_ids = {str(item["server_id"]) for item in eligible}

    def writable_workers_for(path: str) -> list[dict[str, Any]]:
        checks = [
            evaluate_server_path_access(server, path, require_writable=True)
            for server in list_servers(session)
            if server.id in eligible_ids
        ]
        return [item for item in checks if item.get("can_access")]

    output_writers = {str(item["server_id"]) for item in writable_workers_for(request.output_dir)}
    missing_output_writers = sorted(eligible_ids - output_writers)
    if missing_output_writers:
        issues.append(
            _preflight_issue(
                "error",
                "output_path_not_writable",
                "One or more eligible workers cannot confirm write access to output_dir.",
                path=request.output_dir,
                eligible_workers=sorted(eligible_ids),
                writable_workers=sorted(output_writers),
                unwritable_workers=missing_output_writers,
            )
        )
    effective_manifest_root = request.manifest_root or infer_default_manifest_root(
        session,
        input_dir=request.input_dir,
        input_mode=request.input_mode,
        assigned_server_id=request.assigned_server_id,
        allowed_server_ids=request.allowed_server_ids,
    )
    if effective_manifest_root:
        manifest_writers = {str(item["server_id"]) for item in writable_workers_for(effective_manifest_root)}
        missing_manifest_writers = sorted(eligible_ids - manifest_writers)
        if missing_manifest_writers:
            issues.append(
                _preflight_issue(
                    "error",
                    "manifest_root_not_writable",
                    "One or more eligible workers cannot confirm write access to manifest_root.",
                    path=effective_manifest_root,
                    inferred=not bool(request.manifest_root),
                    eligible_workers=sorted(eligible_ids),
                    writable_workers=sorted(manifest_writers),
                    unwritable_workers=missing_manifest_writers,
                )
            )
    versions = _server_versions(session, {str(item["server_id"]) for item in eligible})
    if len(versions) > 1:
        issues.append(
            _preflight_issue(
                "warning",
                "mixed_worker_versions",
                "Selected eligible workers report different git_ref or script_version values.",
                versions=versions,
            )
        )
    constrained_workers = _resource_constrained_workers(session, eligible_ids)
    if constrained_workers:
        issues.append(
            _preflight_issue(
                "warning",
                "resource_constrained_workers",
                "One or more eligible workers currently report resource pressure and may delay claiming work.",
                workers=constrained_workers,
            )
        )
    backlog_workers = _workers_with_event_spool_backlog(session, eligible_ids)
    if backlog_workers:
        issues.append(
            _preflight_issue(
                "warning",
                "worker_event_spool_backlog",
                "One or more eligible workers report unreplayed, quarantined, or dropped local event/log spool records.",
                workers=backlog_workers,
            )
        )
    pending_update_workers = _workers_with_pending_shard_update_backlog(session, eligible_ids)
    if pending_update_workers:
        issues.append(
            _preflight_issue(
                "warning",
                "worker_pending_shard_update_backlog",
                "One or more eligible workers report unreplayed or quarantined local shard progress updates.",
                workers=pending_update_workers,
            )
        )

    if request.model_profile_id:
        profile = session.get(ModelProfile, request.model_profile_id)
        if profile is None:
            issues.append(
                _preflight_issue(
                    "error",
                    "unknown_model_profile",
                    "Selected model profile does not exist.",
                    model_profile_id=request.model_profile_id,
                )
            )
        elif profile.requires_api_key and not (_resolve_model_profile_api_key(profile) or request.extra_args.get("api_key")):
            issues.append(
                _preflight_issue(
                    "error",
                    "model_profile_missing_api_key",
                    "Selected model profile requires an API key, but no saved or per-job key is available.",
                    model_profile_id=request.model_profile_id,
                )
            )
        elif profile.api_key:
            issues.append(
                _preflight_issue(
                    "warning",
                    "model_profile_saved_api_key",
                    "Selected model profile stores a legacy API key in the control database; save the profile with clear_api_key=true and migrate to api_key_env_var.",
                    model_profile_id=request.model_profile_id,
                    api_key_env_var=profile.api_key_env_var,
                )
            )

    if JOB_FILE_DETAIL_LIMIT > 100000 or JOB_EVENT_DETAIL_LIMIT > 100000:
        issues.append(
            _preflight_issue(
                "warning",
                "high_detail_row_limits",
                "Large per-file or raw-event retention limits can grow quickly on million-scale jobs.",
                job_file_detail_limit=JOB_FILE_DETAIL_LIMIT,
                job_event_detail_limit=JOB_EVENT_DETAIL_LIMIT,
            )
        )

    return JobPreflightResponse(
        ok=not any(issue.severity == "error" for issue in issues),
        database_dialect=database_dialect,
        total_workers=len(eligibilities),
        eligible_workers=len(eligible),
        ready_workers=len(ready),
        issues=issues,
    )


def default_manifest_root_for_shared_path(shared_root: str) -> str:
    normalized_root = _normal_posix_path(shared_root).rstrip("/") or "/"
    return posixpath.join(normalized_root, DEFAULT_MANIFEST_ROOT_SUFFIX)


def infer_default_manifest_root(
    session: Session,
    *,
    input_dir: str,
    input_mode: str,
    assigned_server_id: str | None,
    allowed_server_ids: list[str],
) -> str | None:
    if input_mode == "existing_manifest":
        return None
    if input_mode == "directory":
        scoped_server_ids = [assigned_server_id] if assigned_server_id else []
    else:
        scoped_server_ids = allowed_server_ids

    if scoped_server_ids:
        servers = [
            server
            for server_id in scoped_server_ids
            if server_id and server_id != POOL_SERVER_ID
            for server in [session.get(Server, server_id)]
            if server is not None and server.archived_at is None
        ]
    else:
        servers = [server for server in list_servers(session) if server.id != POOL_SERVER_ID]

    matched_roots = {
        item["matched_path"]
        for server in servers
        for item in [evaluate_server_path_access(server, input_dir)]
        if item["can_access"] and item["matched_path"]
    }
    if len(matched_roots) != 1:
        return None
    return default_manifest_root_for_shared_path(next(iter(matched_roots)))


def server_can_access_input_dir(session: Session, server_id: str, input_dir: str) -> bool:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return False
    return bool(evaluate_server_path_access(server, input_dir)["can_access"])


def _manifest_output_dir(job: Job, request: JobCreateRequest) -> Path:
    manifest_root = request.manifest_root or job.manifest_root
    if manifest_root:
        return Path(manifest_root) / job.id
    return Path(job.output_dir) / "_manifest" / job.id


def _manifest_output_dir_for_job(job: Job) -> Path:
    if job.manifest_root:
        return Path(job.manifest_root) / job.id
    return Path(job.output_dir) / "_manifest" / job.id


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


def _create_static_shards_for_job(session: Session, job: Job, request: JobCreateRequest) -> None:
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
        output_dir=_manifest_output_dir(job, request),
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
    freeze_manifest_if_scan_complete(session, job, manifest)


def _create_distributed_scan_for_job(session: Session, job: Job) -> None:
    if job.input_mode not in REMOTE_DISTRIBUTED_SCAN_INPUT_MODES:
        return
    root = _manifest_output_dir_for_job(job)
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


def create_job(session: Session, request: JobCreateRequest) -> Job:
    if request.input_mode not in ALLOWED_INPUT_MODES:
        raise ValueError(f"unknown input_mode: {request.input_mode}")
    migration_issue = _database_migration_preflight_issue(
        database.describe_database_status(session.get_bind())
    )
    if migration_issue is not None:
        raise ValueError(migration_issue.message)
    model_config = _effective_job_model_config(session, request)
    assigned_server_id = request.assigned_server_id
    if request.input_mode == "directory" and not assigned_server_id:
        raise ValueError("assigned_server_id is required for directory input_mode")
    if request.input_mode != "directory" and not assigned_server_id:
        assigned_server_id = ensure_pool_server(session).id
    assigned_server = session.get(Server, assigned_server_id) if assigned_server_id is not None else None
    if assigned_server is None or assigned_server.archived_at is not None:
        raise ValueError(f"unknown assigned server: {assigned_server_id}")
    allowed_server_ids = list(dict.fromkeys(request.allowed_server_ids))
    for server_id in allowed_server_ids:
        server = session.get(Server, server_id)
        if server is None or server.archived_at is not None or server_id == POOL_SERVER_ID:
            raise ValueError(f"unknown allowed server: {server_id}")
    manifest_root = request.manifest_root or infer_default_manifest_root(
        session,
        input_dir=request.input_dir,
        input_mode=request.input_mode,
        assigned_server_id=assigned_server_id,
        allowed_server_ids=allowed_server_ids,
    )

    job = Job(
        input_dir=request.input_dir,
        output_dir=request.output_dir,
        engine=model_config["engine"],
        input_mode=request.input_mode,
        model_profile_id=request.model_profile_id,
        manifest_root=manifest_root,
        target_files_per_shard=request.target_files_per_shard,
        max_shard_attempts=request.max_shard_attempts,
        assigned_server_id=assigned_server_id,
        allowed_server_ids_json=json_dumps(allowed_server_ids),
        engine_config=request.engine_config,
        ip=model_config["ip"],
        port=model_config["port"],
        model_name=model_config["model_name"],
        page_concurrency=model_config["page_concurrency"],
        force_reprocess=request.force_reprocess,
        extra_args_json=json_dumps(model_config["extra_args"]),
    )
    session.add(job)
    try:
        session.flush()
        _create_static_shards_for_job(session, job, request)
        _create_distributed_scan_for_job(session, job)
        session.commit()
    except Exception:
        session.rollback()
        raise
    session.refresh(job)
    return job


def claim_next_job(session: Session, server_id: str) -> Job | None:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return None

    select_stmt = (
        select(Job)
        .where(Job.assigned_server_id == server_id)
        .where(Job.status == "queued")
        .order_by(Job.created_at)
        .limit(1)
    )
    job = session.execute(select_stmt).scalar_one_or_none()
    if job is None:
        return claim_next_pool_job(session, server_id)

    started_at = utcnow()
    claim_stmt = (
        update(Job)
        .where(Job.id == job.id)
        .where(Job.status == "queued")
        .values(status="running", started_at=started_at)
    )
    result = session.execute(claim_stmt)
    if result.rowcount != 1:
        session.rollback()
        return claim_next_job(session, server_id)

    session.commit()
    return session.get(Job, job.id)


def _pool_job_has_claimable_shards(session: Session, job_id: str, now: datetime) -> bool:
    reconcile_expired_shard_leases(session, now=now, job_id=job_id)
    claimable_count = session.execute(
        select(func.count(WorkShard.id))
        .where(WorkShard.job_id == job_id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
    ).scalar_one()
    return bool(claimable_count)


def claim_next_pool_job(session: Session, server_id: str) -> Job | None:
    now = utcnow()
    candidates = session.execute(
        select(Job)
        .where(Job.assigned_server_id == POOL_SERVER_ID)
        .where(Job.status.in_({"queued", "running"}))
        .order_by(Job.created_at)
    ).scalars().all()
    for job in candidates:
        if not server_is_allowed_for_job(job, server_id):
            continue
        if not server_can_access_input_dir(session, server_id, job.input_dir):
            continue
        if job.input_mode in REMOTE_STATIC_INPUT_MODES and job.status == "queued":
            claim_stmt = (
                update(Job)
                .where(Job.id == job.id)
                .where(Job.status == "queued")
                .values(status="running", started_at=now)
            )
            result = session.execute(claim_stmt)
            if result.rowcount != 1:
                session.rollback()
                return claim_next_job(session, server_id)
            session.commit()
            return session.get(Job, job.id)
        if not _pool_job_has_claimable_shards(session, job.id, now):
            continue
        if job.status == "queued":
            claim_stmt = (
                update(Job)
                .where(Job.id == job.id)
                .where(Job.status == "queued")
                .values(status="running", started_at=now)
            )
            result = session.execute(claim_stmt)
            if result.rowcount != 1:
                session.rollback()
                return claim_next_job(session, server_id)
            session.commit()
            return session.get(Job, job.id)
        return job
    return None


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
    session.commit()
    session.refresh(manifest)
    return manifest


def _claimable_scan_unit_id_select(
    *,
    limit: int = SCAN_UNIT_CLAIM_BATCH_SIZE,
    after_id: int | None = None,
):
    statement = (
        select(ScanUnit)
        .join(Job, ScanUnit.job_id == Job.id)
        .where(ScanUnit.status.in_(RECLAIMABLE_SCAN_UNIT_STATUSES))
        .where(Job.assigned_server_id == POOL_SERVER_ID)
        .where(Job.status.in_({"queued", "running"}))
    )
    if after_id is not None:
        statement = statement.where(ScanUnit.id > after_id)
    return (
        statement.order_by(ScanUnit.id.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
        .with_only_columns(ScanUnit.id)
    )


def claim_next_scan_unit(session: Session, server_id: str) -> ScanUnit | None:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return None
    now = utcnow()
    reconcile_expired_scan_unit_leases(session, now=now)
    after_id: int | None = None
    while True:
        candidate_ids = session.execute(
            _claimable_scan_unit_id_select(
                limit=SCAN_UNIT_CLAIM_BATCH_SIZE,
                after_id=after_id,
            )
        ).scalars().all()
        if not candidate_ids:
            return None
        after_id = max(candidate_ids)
        for unit_id in candidate_ids:
            unit = session.get(ScanUnit, unit_id)
            if unit is None:
                continue
            job = unit.job
            if not server_is_allowed_for_job(job, server_id):
                continue
            if not server_can_access_input_dir(session, server_id, unit.path):
                continue
            result = session.execute(
                update(ScanUnit)
                .where(ScanUnit.id == unit.id)
                .where(ScanUnit.status.in_(RECLAIMABLE_SCAN_UNIT_STATUSES))
                .values(
                    status="running",
                    assigned_server_id=server_id,
                    attempt_count=ScanUnit.attempt_count + 1,
                    started_at=now,
                    lease_expires_at=scan_unit_lease_deadline(now),
                    failure_category=None,
                    error_message=None,
                )
            )
            if result.rowcount != 1:
                session.rollback()
                return claim_next_scan_unit(session, server_id)
            if job.status == "queued":
                job.status = "running"
                job.started_at = now
            session.commit()
            return session.get(ScanUnit, unit.id)
        session.rollback()


def _next_manifest_shard_index(
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
        select(func.max(WorkShard.shard_index)).where(WorkShard.manifest_id == manifest.id)
    ).scalar_one()
    return max(stored_next, int(existing_max or 0) + 1)


def _manifest_for_scan_unit_completion_select(job_id: str):
    return (
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
        .with_for_update()
    )


def _existing_scan_unit_paths(session: Session, job_id: str, paths: list[str]) -> set[str]:
    if not paths:
        return set()
    rows = session.execute(
        select(ScanUnit.path)
        .where(ScanUnit.job_id == job_id)
        .where(ScanUnit.path.in_(paths))
    ).scalars().all()
    return {str(path) for path in rows}


def complete_scan_unit(
    session: Session,
    scan_unit_id: int,
    request: ScanUnitCompleteRequest,
) -> ScanUnit:
    unit = session.execute(
        select(ScanUnit)
        .where(ScanUnit.id == scan_unit_id)
        .with_for_update()
    ).scalar_one_or_none()
    if unit is None:
        raise ValueError(f"unknown scan unit: {scan_unit_id}")
    if request.assigned_server_id is not None and request.assigned_server_id != unit.assigned_server_id:
        raise ScanUnitAttemptConflictError(
            "scan unit completion belongs to a different server attempt"
        )
    if request.attempt_count is not None and request.attempt_count != unit.attempt_count:
        raise ScanUnitAttemptConflictError(
            "scan unit completion belongs to a stale attempt"
        )
    if unit.status == "succeeded":
        session.commit()
        session.refresh(unit)
        return unit
    if unit.status != "running":
        raise ScanUnitAttemptConflictError(
            f"scan unit is not running: {unit.status}"
        )
    job = get_job_or_raise(session, unit.job_id)
    manifest = session.execute(_manifest_for_scan_unit_completion_select(job.id)).scalar_one()
    unit.status = "succeeded"
    unit.manifest_path = request.manifest_path
    unit.meta_path = request.meta_path
    unit.file_count = request.file_count
    unit.total_bytes = request.total_bytes
    unit.finished_at = utcnow()
    unit.lease_expires_at = None
    child_paths = list(dict.fromkeys(request.child_paths))
    existing_child_paths = _existing_scan_unit_paths(session, job.id, child_paths)
    for child_path in child_paths:
        if child_path in existing_child_paths:
            continue
        session.add(ScanUnit(job_id=job.id, path=child_path, status="pending"))
    next_shard_index = _next_manifest_shard_index(session, manifest, len(request.shards))
    for offset, shard in enumerate(request.shards, start=1):
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=next_shard_index + offset - 1,
                shard_path=shard.shard_path,
                status="pending",
                file_count=shard.file_count,
            )
        )
    manifest.next_shard_index = next_shard_index + len(request.shards)
    manifest.file_count = int(manifest.file_count or 0) + request.file_count
    manifest.total_bytes = int(manifest.total_bytes or 0) + request.total_bytes
    session.flush()
    freeze_manifest_if_scan_complete(session, job, manifest)
    session.commit()
    session.refresh(unit)
    return unit


def fail_scan_unit(
    session: Session,
    scan_unit_id: int,
    request: ScanUnitFailRequest,
) -> ScanUnit:
    unit = session.execute(
        select(ScanUnit)
        .where(ScanUnit.id == scan_unit_id)
        .with_for_update()
    ).scalar_one_or_none()
    if unit is None:
        raise ValueError(f"unknown scan unit: {scan_unit_id}")
    if request.assigned_server_id is not None and request.assigned_server_id != unit.assigned_server_id:
        raise ScanUnitAttemptConflictError(
            "scan unit failure belongs to a different server attempt"
        )
    if request.attempt_count is not None and request.attempt_count != unit.attempt_count:
        raise ScanUnitAttemptConflictError(
            "scan unit failure belongs to a stale attempt"
        )
    if unit.status == "failed":
        session.commit()
        session.refresh(unit)
        return unit
    if unit.status != "running":
        raise ScanUnitAttemptConflictError(
            f"scan unit is not running: {unit.status}"
        )
    job = get_job_or_raise(session, unit.job_id)
    now = utcnow()
    unit.status = "failed"
    unit.failure_category = _scan_unit_failure_category(request)
    unit.error_message = request.error_message
    unit.finished_at = now
    unit.lease_expires_at = None
    session.flush()

    active_units = session.execute(
        select(func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status.in_({"pending", "running", "stale"}))
    ).scalar_one()
    if int(active_units or 0) == 0:
        manifest = session.execute(
            select(Manifest)
            .where(Manifest.job_id == job.id)
            .order_by(Manifest.id.asc())
            .limit(1)
        ).scalar_one_or_none()
        if manifest is not None:
            manifest.status = "failed"
    session.commit()
    session.refresh(unit)
    return unit


def archive_server(session: Session, server_id: str) -> None:
    if server_id == POOL_SERVER_ID:
        raise ServerArchiveError("The internal server pool cannot be archived.")
    server = session.get(Server, server_id)
    if server is None:
        raise UnknownServerError(f"Unknown server: {server_id}")
    if server.archived_at is not None:
        return
    if effective_server_status(server) != "offline":
        raise ServerArchiveError("Only offline or stale servers can be archived.")
    stop_assigned_queued_jobs_for_server(session, server_id)
    if count_open_jobs_for_server(session, server_id) > 0 or count_running_shards_for_server(session, server_id) > 0:
        raise ServerArchiveError("Server still has active work.")

    server.archived_at = utcnow()
    session.commit()


def list_servers(session: Session, *, include_archived: bool = False) -> list[Server]:
    stmt = select(Server).order_by(Server.id.asc())
    if not include_archived:
        stmt = stmt.where(Server.archived_at.is_(None))
    return list(session.execute(stmt).scalars().all())


def _normalized_status_filter(status: str | None) -> str | None:
    if status is None:
        return None
    normalized = status.strip().lower()
    if not normalized or normalized == "all":
        return None
    if normalized not in JOB_STATUS_FILTERS:
        allowed = ", ".join(sorted(JOB_STATUS_FILTERS))
        raise ValueError(
            f"unknown job status filter: {status}; allowed values: all, {allowed}"
        )
    return normalized


def _normalized_shard_status_filter(status: str | None) -> str:
    normalized = status.strip().lower() if status else "all"
    if not normalized:
        normalized = "all"
    if normalized not in SHARD_STATUS_FILTERS:
        allowed = ", ".join(sorted(SHARD_STATUS_FILTERS))
        raise ValueError(
            f"unknown shard status filter: {status}; allowed values: {allowed}"
        )
    return normalized


def list_job_summaries(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
) -> list[JobSummaryResponse]:
    stmt = select(Job).order_by(Job.created_at.desc())
    status = _normalized_status_filter(status)
    if status:
        stmt = stmt.where(Job.status == status)
    if not include_archived:
        stmt = stmt.where(Job.archived_at.is_(None))
    stmt = stmt.offset(max(offset, 0)).limit(max(limit, 1))
    jobs = session.execute(stmt).scalars().all()
    return [get_job_summary(session, job) for job in jobs]


def list_job_summaries_page(
    session: Session,
    *,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    include_archived: bool = False,
) -> JobSummaryListResponse:
    limit = max(limit, 1)
    offset = max(offset, 0)
    status = _normalized_status_filter(status)
    count_stmt = select(func.count(Job.id))
    item_stmt = select(Job).order_by(Job.created_at.desc())
    if status:
        count_stmt = count_stmt.where(Job.status == status)
        item_stmt = item_stmt.where(Job.status == status)
    if not include_archived:
        count_stmt = count_stmt.where(Job.archived_at.is_(None))
        item_stmt = item_stmt.where(Job.archived_at.is_(None))
    total = int(session.execute(count_stmt).scalar_one() or 0)
    jobs = session.execute(item_stmt.offset(offset).limit(limit)).scalars().all()
    return JobSummaryListResponse(
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(jobs) < total,
        items=[get_job_summary(session, job) for job in jobs],
    )


def _static_input_file_count(session: Session, job_id: str) -> int:
    manifest_file_count = session.execute(
        select(func.max(Manifest.file_count)).where(Manifest.job_id == job_id)
    ).scalar_one()
    shard_file_count = session.execute(
        select(func.coalesce(func.sum(WorkShard.file_count), 0)).where(
            WorkShard.job_id == job_id
        )
    ).scalar_one()
    return max(int(manifest_file_count or 0), int(shard_file_count or 0))


def _latest_manifest_scan_progress(session: Session, job_id: str) -> dict[str, Any]:
    event = session.execute(
        select(JobEvent)
        .where(JobEvent.job_id == job_id)
        .where(JobEvent.event_type == "manifest_scan_progress")
        .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if event is None:
        return {}
    return json_loads_object(event.payload_json)


def _manifest_scan_metadata(manifest: Manifest | None) -> dict[str, Any]:
    if manifest is None or not manifest.meta_path:
        return {}
    try:
        payload = json.loads(Path(manifest.meta_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _manifest_scan_error_samples(manifest_meta: dict[str, Any], *, limit: int = 5) -> list[dict[str, Any]]:
    samples: list[dict[str, Any]] = []
    for item in manifest_meta.get("skipped_errors") or []:
        if not isinstance(item, dict):
            continue
        samples.append(_scan_error_sample_with_category(item))
        if len(samples) >= limit:
            break
    return samples


def _recent_manifest_scan_error_samples(session: Session, job_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    rows = session.execute(
        select(JobEvent)
        .where(JobEvent.job_id == job_id)
        .where(JobEvent.event_type == "manifest_scan_progress")
        .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
        .limit(50)
    ).scalars().all()
    samples: list[dict[str, Any]] = []
    seen_keys: set[tuple[str, str]] = set()
    for event in rows:
        payload = json_loads_object(event.payload_json)
        for item in payload.get("skipped_errors") or []:
            if not isinstance(item, dict):
                continue
            sample = _scan_error_sample_with_category(item)
            key = (str(sample.get("path") or ""), str(sample.get("reason") or ""))
            if key in seen_keys:
                continue
            seen_keys.add(key)
            samples.append(sample)
            if len(samples) >= limit:
                return samples
    return samples


def _scan_unit_problem_samples(session: Session, job_id: str, *, limit: int = 5) -> list[dict[str, Any]]:
    return [
        {
            "path": path,
            "reason": error_message or f"scan unit {status}",
            "failure_category": failure_category,
        }
        for path, status, error_message, failure_category in session.execute(
            select(ScanUnit.path, ScanUnit.status, ScanUnit.error_message, ScanUnit.failure_category)
            .where(ScanUnit.job_id == job_id)
            .where(ScanUnit.status.in_({"failed", "stale"}))
            .order_by(ScanUnit.id.asc())
            .limit(limit)
        ).all()
    ]


def _manifest_scan_started_at(session: Session, job_id: str) -> datetime | None:
    return session.execute(
        select(func.min(JobEvent.created_at))
        .where(JobEvent.job_id == job_id)
        .where(JobEvent.event_type == "manifest_scan_progress")
    ).scalar_one()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _scan_eta_seconds(
    *,
    started_at: datetime | None,
    now: datetime,
    scanned_files: int,
    estimated_total_files: int | None,
) -> int | None:
    if started_at is None or estimated_total_files is None:
        return None
    if scanned_files <= 0 or estimated_total_files <= scanned_files:
        return None
    elapsed_seconds = max((now - started_at).total_seconds(), 0.0)
    if elapsed_seconds <= 0:
        return None
    files_per_second = scanned_files / elapsed_seconds
    if files_per_second <= 0:
        return None
    return int((estimated_total_files - scanned_files) / files_per_second)


def _scan_eta_seconds_from_rate(
    *,
    scanned_files: int,
    estimated_total_files: int | None,
    files_per_second: Any,
) -> int | None:
    if estimated_total_files is None:
        return None
    if scanned_files <= 0 or estimated_total_files <= scanned_files:
        return None
    try:
        rate = float(files_per_second)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate <= 0:
        return None
    return int((estimated_total_files - scanned_files) / rate)


def _scan_unit_eta_seconds(
    *,
    started_at: datetime | None,
    now: datetime,
    completed_units: int,
    total_units: int,
) -> int | None:
    if started_at is None or completed_units <= 0 or total_units <= completed_units:
        return None
    elapsed_seconds = max((now - started_at).total_seconds(), 0.0)
    if elapsed_seconds <= 0:
        return None
    units_per_second = completed_units / elapsed_seconds
    if units_per_second <= 0:
        return None
    return int((total_units - completed_units) / units_per_second)


def _shard_lease_status(shard: WorkShard, now: datetime) -> tuple[str, int | None]:
    if shard.status == "stale":
        return "stale", 0
    if shard.status not in CURRENT_WORKER_SHARD_STATUSES:
        return "none", None
    if shard.lease_expires_at is None:
        return "missing", None
    remaining = int((shard.lease_expires_at - now).total_seconds())
    if remaining <= 0:
        return "expired", 0
    if remaining <= 30:
        return "expiring", remaining
    return "healthy", remaining


def _shard_progress_summary(shard: WorkShard, job: Job, now: datetime) -> JobShardProgressSummary:
    lease_status, lease_seconds_remaining = _shard_lease_status(shard, now)
    elapsed_seconds = 0.0
    if shard.started_at is not None:
        elapsed_seconds = max((now - shard.started_at).total_seconds(), 0.0)
    pages_per_second = None
    files_per_minute = None
    if elapsed_seconds > 0:
        if shard.completed_pages > 0:
            pages_per_second = round(shard.completed_pages / elapsed_seconds, 4)
        if shard.processed_files > 0:
            files_per_minute = round(shard.processed_files / elapsed_seconds * 60, 4)
    return JobShardProgressSummary(
        id=shard.id,
        shard_index=shard.shard_index,
        status=shard.status,
        assigned_server_id=shard.assigned_server_id,
        started_at=shard.started_at,
        running_seconds=round(elapsed_seconds, 1) if shard.started_at is not None else None,
        file_count=shard.file_count,
        processed_files=shard.processed_files,
        failed_files=shard.failed_files,
        skipped_files=shard.skipped_files,
        completed_pages=shard.completed_pages,
        api_inflight=shard.api_inflight,
        api_inflight_peak=shard.api_inflight_peak,
        api_waiting=shard.api_waiting,
        oldest_api_inflight=round(shard.oldest_api_inflight, 4),
        execution_paused=shard.execution_paused,
        api_concurrency_limit=shard.api_concurrency_limit,
        execution_control_reason=shard.execution_control_reason,
        pages_per_second=pages_per_second,
        files_per_minute=files_per_minute,
        attempt_count=shard.attempt_count,
        max_attempts=job.max_shard_attempts,
        lease_expires_at=shard.lease_expires_at,
        lease_seconds_remaining=lease_seconds_remaining,
        lease_status=lease_status,
        failure_category=shard.failure_category,
        error_message=shard.error_message,
    )


def _job_lifecycle_stage(
    *,
    job: Job,
    scan_status: str,
    total_shards: int,
    running_shards: int,
    retrying_shards: int,
    stale_shards: int,
    failed_shards: int,
    stopped_shards: int,
    pending_scan_units: int,
    running_scan_units: int,
    stale_scan_units: int,
    failed_scan_units: int,
) -> str:
    if job.status in TERMINAL_JOB_STATUSES:
        return job.status
    if job.stop_requested or job.status == "stopping":
        return "draining"
    if failed_shards or stopped_shards or failed_scan_units:
        return "failed"
    if retrying_shards or stale_shards or stale_scan_units:
        return "recovering"
    if pending_scan_units or running_scan_units or scan_status == "running":
        return "scanning"
    if scan_status == "done" and total_shards == 0:
        return "sharding"
    if running_shards or total_shards:
        return "running"
    return job.status


def _manifest_snapshot_status(manifest: Manifest | None) -> str:
    if manifest is None:
        return "missing"
    if manifest.frozen_at is not None:
        return "frozen"
    if manifest.status == "scanning":
        return "scanning"
    if manifest.status == "ready":
        return "ready"
    return manifest.status or "unknown"


def _manifest_freeze_integrity_summary(manifest: Manifest | None) -> dict[str, Any]:
    if manifest is None or manifest.frozen_at is None:
        if manifest is not None:
            worker_report = _load_worker_integrity_report(manifest)
            if worker_report is not None:
                worker_summary = _manifest_integrity_freeze_summary(worker_report)
                return {
                    "manifest_integrity_status": worker_report.status,
                    "manifest_integrity_ok": worker_report.ok,
                    "manifest_integrity_issue_count": worker_summary["integrity_issue_count"],
                }
        return {
            "manifest_integrity_status": None,
            "manifest_integrity_ok": None,
            "manifest_integrity_issue_count": 0,
        }
    worker_report = _load_worker_integrity_report(manifest)
    if worker_report is not None:
        worker_summary = _manifest_integrity_freeze_summary(worker_report)
        return {
            "manifest_integrity_status": worker_report.status,
            "manifest_integrity_ok": worker_report.ok,
            "manifest_integrity_issue_count": worker_summary["integrity_issue_count"],
        }
    try:
        report = json_loads_object(manifest.freeze_report_json)
    except json.JSONDecodeError:
        return {
            "manifest_integrity_status": "invalid_freeze_report",
            "manifest_integrity_ok": False,
            "manifest_integrity_issue_count": 1,
        }
    return {
        "manifest_integrity_status": report.get("integrity_status"),
        "manifest_integrity_ok": report.get("integrity_ok"),
        "manifest_integrity_issue_count": int(report.get("integrity_issue_count") or 0),
    }


def get_job_summary(session: Session, job_or_id: Job | str) -> JobSummaryResponse:
    job = get_job_or_raise(session, job_or_id) if isinstance(job_or_id, str) else job_or_id
    reconcile_expired_shard_leases(session, job_id=job.id)
    reconcile_expired_scan_unit_leases(session, job_id=job.id)
    session.flush()
    finalize_stopped_job_if_idle(session, job)
    session.flush()
    session.commit()
    summary_now = utcnow()
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job.id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    file_rows = session.execute(
        select(
            func.count(JobFile.id),
            func.coalesce(
                func.sum(JobFile.status.in_(COMPLETED_FILE_STATUSES).cast(Integer)),
                0,
            ),
            func.coalesce(
                func.sum(JobFile.status.in_(FAILED_FILE_STATUSES).cast(Integer)),
                0,
            ),
            func.coalesce(
                func.sum(JobFile.status.in_(SKIPPED_FILE_STATUSES).cast(Integer)),
                0,
            ),
            func.coalesce(func.sum(JobFile.done_pages), 0),
            func.sum(JobFile.total_pages),
        ).where(JobFile.job_id == job.id)
    ).one()
    observed_total_files = int(file_rows[0] or 0)
    counter = session.get(JobCounter, job.id)
    static_input_files = _static_input_file_count(session, job.id)
    authoritative_total_files = max(observed_total_files, static_input_files)
    counter_total_files = _job_counter_total_files(counter)
    total_files = authoritative_total_files or counter_total_files
    scanned_files = total_files
    completed_files = max(int(file_rows[1] or 0), counter.completed_files if counter else 0)
    failed_files = max(int(file_rows[2] or 0), counter.failed_files if counter else 0)
    skipped_files = max(int(file_rows[3] or 0), counter.skipped_files if counter else 0)
    observed_completed_pages = int(file_rows[4] or 0)
    completed_pages = (
        observed_completed_pages
        if observed_total_files
        else max(observed_completed_pages, counter.completed_pages if counter else 0)
    )
    event_total_pages = counter.total_pages if counter and counter.total_pages > 0 else None
    if file_rows[5] is not None and event_total_pages is not None:
        total_pages = max(int(file_rows[5]), event_total_pages)
    elif file_rows[5] is not None:
        total_pages = int(file_rows[5])
    else:
        total_pages = event_total_pages
    shard_progress_rows = session.execute(
        select(
            func.coalesce(func.sum(WorkShard.processed_files), 0),
            func.coalesce(func.sum(WorkShard.failed_files), 0),
            func.coalesce(func.sum(WorkShard.skipped_files), 0),
            func.coalesce(func.sum(WorkShard.completed_pages), 0),
        ).where(WorkShard.job_id == job.id)
    ).one()
    shard_processed_files = int(shard_progress_rows[0] or 0)
    shard_failed_files = int(shard_progress_rows[1] or 0)
    shard_skipped_files = int(shard_progress_rows[2] or 0)
    shard_completed_files = max(
        shard_processed_files - shard_failed_files - shard_skipped_files,
        0,
    )
    completed_files = max(completed_files, shard_completed_files)
    failed_files = max(failed_files, shard_failed_files)
    skipped_files = max(skipped_files, shard_skipped_files)
    if not observed_total_files:
        completed_pages = max(completed_pages, int(shard_progress_rows[3] or 0))
    if total_files:
        failed_files = min(failed_files, total_files)
        skipped_files = min(skipped_files, max(total_files - failed_files, 0))
        completed_files = min(
            completed_files,
            max(total_files - failed_files - skipped_files, 0),
        )
    if total_pages:
        completed_pages = min(completed_pages, total_pages)
    failure_category_counts = _load_failure_category_counts(counter)
    scan_progress = _latest_manifest_scan_progress(session, job.id)
    scan_progress_files = int(scan_progress.get("scanned_files") or 0)
    scan_progress_dirs = int(scan_progress.get("scanned_dirs") or 0)
    scan_progress_bytes = int(scan_progress.get("total_bytes") or 0)
    scan_error_samples = _recent_manifest_scan_error_samples(session, job.id)
    scan_error_count = int(scan_progress.get("skipped_error_count") or len(scan_error_samples))
    manifest_scan_meta = _manifest_scan_metadata(manifest)
    if manifest is not None and manifest_scan_meta:
        scan_progress_files = max(scan_progress_files, int(manifest.file_count or 0))
        try:
            manifest_scan_dirs = int(
                manifest_scan_meta.get("scanned_dir_count")
                or manifest_scan_meta.get("scanned_dirs")
                or 0
            )
        except (TypeError, ValueError):
            manifest_scan_dirs = 0
        scan_progress_dirs = max(scan_progress_dirs, manifest_scan_dirs)
        scan_progress_bytes = max(scan_progress_bytes, int(manifest.total_bytes or 0))
        try:
            manifest_scan_error_count = int(manifest_scan_meta.get("skipped_error_count") or 0)
        except (TypeError, ValueError):
            manifest_scan_error_count = 0
        scan_error_count = max(scan_error_count, manifest_scan_error_count)
        if not scan_error_samples:
            scan_error_samples = _manifest_scan_error_samples(manifest_scan_meta)
    scanned_files = max(scanned_files, scan_progress_files)

    last_event_at = session.execute(
        select(func.max(JobEvent.created_at)).where(JobEvent.job_id == job.id)
    ).scalar_one()
    if last_event_at is None and counter is not None:
        last_event_at = counter.last_event_at
    first_event_at = session.execute(
        select(func.min(JobEvent.created_at)).where(JobEvent.job_id == job.id)
    ).scalar_one()
    if first_event_at is None and counter is not None:
        first_event_at = counter.first_event_at
    last_heartbeat_at = session.execute(
        select(func.max(JobEvent.created_at))
        .where(JobEvent.job_id == job.id)
        .where(JobEvent.event_type == "job_heartbeat")
    ).scalar_one()
    degraded_pages = int(
        session.execute(
            select(func.count(JobEvent.id))
            .where(JobEvent.job_id == job.id)
            .where(JobEvent.event_type == "page_done")
            .where(JobEvent.status.in_(DEGRADED_PAGE_STATUSES))
        ).scalar_one()
        or 0
    )
    if counter is not None:
        degraded_pages = max(degraded_pages, counter.degraded_pages)
    quality_flags = ["image_fallback"] if degraded_pages > 0 else []
    shard_rows = session.execute(
        select(WorkShard.status, func.count(WorkShard.id))
        .where(WorkShard.job_id == job.id)
        .group_by(WorkShard.status)
    ).all()
    shard_counts = {status: int(count) for status, count in shard_rows}
    total_shards = sum(shard_counts.values())
    shard_failure_category_rows = session.execute(
        select(WorkShard.failure_category, func.count(WorkShard.id))
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.failure_category.is_not(None))
        .group_by(WorkShard.failure_category)
    ).all()
    shard_failure_category_counts = {
        str(category): int(count)
        for category, count in shard_failure_category_rows
        if category
    }
    scan_unit_rows = session.execute(
        select(ScanUnit.status, func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .group_by(ScanUnit.status)
    ).all()
    scan_unit_counts = {status: int(count) for status, count in scan_unit_rows}
    total_scan_units = sum(scan_unit_counts.values())
    scan_unit_failure_category_rows = session.execute(
        select(ScanUnit.failure_category, func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.failure_category.is_not(None))
        .group_by(ScanUnit.failure_category)
    ).all()
    scan_unit_failure_category_counts = {
        str(category): int(count)
        for category, count in scan_unit_failure_category_rows
        if category
    }
    if scan_unit_counts:
        scan_progress_files = max(scan_progress_files, total_files)
        scan_progress_dirs = max(
            scan_progress_dirs,
            scan_unit_counts.get("succeeded", 0) + scan_unit_counts.get("failed", 0),
        )
        manifest_total_bytes = int(
            session.execute(
                select(func.coalesce(func.sum(Manifest.total_bytes), 0)).where(
                    Manifest.job_id == job.id
                )
            ).scalar_one()
            or 0
        )
        scan_progress_bytes = max(scan_progress_bytes, manifest_total_bytes)
        scan_unit_problem_samples = _scan_unit_problem_samples(session, job.id, limit=5)
        if not scan_error_samples:
            scan_error_samples = scan_unit_problem_samples
        scan_error_count = max(
            scan_error_count,
            scan_unit_counts.get("failed", 0),
            scan_unit_counts.get("stale", 0),
        )
    worker_shard_rows = session.execute(
        select(WorkShard.assigned_server_id, WorkShard.status, func.count(WorkShard.id))
        .where(WorkShard.job_id == job.id)
        .group_by(WorkShard.assigned_server_id, WorkShard.status)
    ).all()
    worker_counts: dict[str | None, dict[str, int]] = {}
    for server_id, status, count in worker_shard_rows:
        counts = worker_counts.setdefault(server_id, {})
        counts[status] = int(count)
    attention_shard_priority = case(
        (WorkShard.status == "running", 0),
        (WorkShard.status == "retrying", 1),
        (WorkShard.status == "stale", 2),
        (WorkShard.status == "failed", 3),
        else_=9,
    )
    attention_shard_stmt = (
        select(WorkShard)
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.status.in_(ATTENTION_SHARD_STATUSES))
        .order_by(attention_shard_priority, WorkShard.shard_index.asc())
    )
    if JOB_SUMMARY_ATTENTION_SHARD_LIMIT:
        attention_shard_stmt = attention_shard_stmt.limit(JOB_SUMMARY_ATTENTION_SHARD_LIMIT)
    attention_shard_rows = list(session.execute(attention_shard_stmt).scalars().all())
    attention_shard_rows.sort(
        key=lambda shard: (
            {"running": 0, "retrying": 1, "stale": 2, "failed": 3}.get(shard.status, 9),
            shard.shard_index,
        )
    )
    attention_shards = [
        _shard_progress_summary(shard, job, summary_now) for shard in attention_shard_rows
    ]
    current_shards_by_worker: dict[str | None, list[JobShardProgressSummary]] = {}
    for shard_summary in attention_shards:
        if shard_summary.status in CURRENT_WORKER_SHARD_STATUSES:
            current_shards_by_worker.setdefault(shard_summary.assigned_server_id, []).append(shard_summary)
    worker_shards = [
        JobWorkerShardSummary(
            server_id=server_id,
            total_shards=sum(counts.values()),
            pending_shards=counts.get("pending", 0),
            running_shards=counts.get("running", 0),
            retrying_shards=counts.get("retrying", 0),
            stale_shards=counts.get("stale", 0),
            succeeded_shards=counts.get("succeeded", 0),
            failed_shards=counts.get("failed", 0),
            stopped_shards=counts.get("stopped", 0),
            current_shards=current_shards_by_worker.get(server_id, []),
            api_inflight=sum(
                shard.api_inflight for shard in current_shards_by_worker.get(server_id, [])
            ),
            api_inflight_peak=max(
                (shard.api_inflight_peak for shard in current_shards_by_worker.get(server_id, [])),
                default=0,
            ),
            api_waiting=sum(
                shard.api_waiting for shard in current_shards_by_worker.get(server_id, [])
            ),
            oldest_api_inflight=max(
                (
                    shard.oldest_api_inflight
                    for shard in current_shards_by_worker.get(server_id, [])
                ),
                default=0.0,
            ),
            execution_paused=any(
                shard.execution_paused for shard in current_shards_by_worker.get(server_id, [])
            ),
        )
        for server_id, counts in sorted(
            worker_counts.items(),
            key=lambda item: (
                item[0] is None,
                item[0] or "",
            ),
        )
    ]

    progress_percent = None
    if total_pages and total_pages > 0:
        progress_percent = round(min(completed_pages / total_pages * 100, 100), 2)
    elif total_files:
        processed_files = completed_files + failed_files + skipped_files
        progress_percent = round(min(processed_files / total_files * 100, 100), 2)

    started_at = job.started_at or first_event_at or job.created_at
    ended_at = summary_now
    if job.status in TERMINAL_JOB_STATUSES:
        ended_at = job.finished_at or last_event_at or ended_at
    elapsed_seconds = max((ended_at - started_at).total_seconds(), 0.0)
    pages_per_second = None
    files_per_minute = None
    eta_seconds = None
    if elapsed_seconds > 0:
        processed_files = completed_files + failed_files + skipped_files
        if completed_pages > 0:
            pages_per_second = round(completed_pages / elapsed_seconds, 4)
        if processed_files > 0:
            files_per_minute = round(processed_files / elapsed_seconds * 60, 4)
        if pages_per_second and total_pages and completed_pages < total_pages:
            eta_seconds = int((total_pages - completed_pages) / pages_per_second)

    freshness_at = last_heartbeat_at or last_event_at or job.started_at
    is_stale = False
    if job.status in {"running", "stopping"} and freshness_at is not None:
        is_stale = summary_now - freshness_at > timedelta(seconds=STALE_AFTER_SECONDS)

    retrying_shards = shard_counts.get("retrying", 0)
    stale_shards = shard_counts.get("stale", 0)
    failed_shards = shard_counts.get("failed", 0)
    stopped_shards = shard_counts.get("stopped", 0)
    stale_scan_units = scan_unit_counts.get("stale", 0)
    failed_scan_units = scan_unit_counts.get("failed", 0)
    scan_status = "not_started"
    if scan_progress:
        scan_status = str(scan_progress.get("status") or "running")
    if scan_unit_counts:
        open_scan_units = (
            scan_unit_counts.get("pending", 0)
            + scan_unit_counts.get("running", 0)
            + scan_unit_counts.get("stale", 0)
        )
        if open_scan_units:
            scan_status = "running"
        elif failed_scan_units:
            scan_status = "failed"
        else:
            scan_status = "done"
    elif manifest is not None:
        if manifest.status == "scanning":
            scan_status = "running"
        elif manifest.frozen_at is not None or manifest.status == "ready":
            scan_status = "done"
    estimated_total_files_raw = scan_progress.get("estimated_total_files")
    try:
        estimated_total_files = int(estimated_total_files_raw) if estimated_total_files_raw is not None else None
    except (TypeError, ValueError):
        estimated_total_files = None
    scan_remaining_files = _optional_int(scan_progress.get("remaining_files"))
    if scan_remaining_files is None and estimated_total_files is not None:
        scan_remaining_files = max(estimated_total_files - scan_progress_files, 0)
    scan_progress_percent = None
    if estimated_total_files is not None and estimated_total_files > 0:
        scan_progress_percent = round(
            min(scan_progress_files / estimated_total_files * 100, 100),
            2,
        )
    scan_started_at = _parse_datetime(scan_progress.get("scan_started_at"))
    if scan_started_at is None:
        scan_started_at = _parse_datetime(manifest_scan_meta.get("scan_started_at"))
    if scan_started_at is None:
        scan_started_at = _manifest_scan_started_at(session, job.id)
    if scan_started_at is None and scan_unit_counts:
        scan_started_at = session.execute(
            select(func.min(ScanUnit.started_at))
            .where(ScanUnit.job_id == job.id)
            .where(ScanUnit.started_at.is_not(None))
        ).scalar_one()
    if scan_started_at is None and scan_unit_counts:
        scan_started_at = job.started_at or job.created_at
    scan_finished_at = _parse_datetime(scan_progress.get("scan_finished_at"))
    if scan_finished_at is None:
        scan_finished_at = _parse_datetime(manifest_scan_meta.get("scan_finished_at"))
    if scan_finished_at is None and manifest is not None and manifest.frozen_at is not None:
        scan_finished_at = manifest.frozen_at
    recovery_status = "healthy"
    if failed_shards or stopped_shards or failed_scan_units:
        recovery_status = "exhausted"
    elif retrying_shards or stale_shards or stale_scan_units:
        recovery_status = "recovering"
    pending_scan_units = scan_unit_counts.get("pending", 0)
    running_scan_units = scan_unit_counts.get("running", 0)
    succeeded_scan_units = scan_unit_counts.get("succeeded", 0)
    executable_shards = (
        shard_counts.get("pending", 0)
        + shard_counts.get("running", 0)
        + retrying_shards
        + stale_shards
    )
    lifecycle_stage = _job_lifecycle_stage(
        job=job,
        scan_status=scan_status,
        total_shards=total_shards,
        running_shards=shard_counts.get("running", 0),
        retrying_shards=retrying_shards,
        stale_shards=stale_shards,
        failed_shards=failed_shards,
        stopped_shards=stopped_shards,
        pending_scan_units=pending_scan_units,
        running_scan_units=running_scan_units,
        stale_scan_units=stale_scan_units,
        failed_scan_units=failed_scan_units,
    )
    completed_scan_units = succeeded_scan_units + failed_scan_units
    scan_eta_seconds = None
    if scan_status == "running":
        scan_eta_seconds = _optional_int(scan_progress.get("estimated_remaining_seconds"))
        if scan_eta_seconds is None:
            scan_eta_seconds = _scan_eta_seconds_from_rate(
                scanned_files=scan_progress_files,
                estimated_total_files=estimated_total_files,
                files_per_second=scan_progress.get("files_per_second"),
            )
        if scan_eta_seconds is None:
            scan_eta_seconds = _scan_eta_seconds(
                started_at=scan_started_at,
                now=summary_now,
                scanned_files=scan_progress_files,
                estimated_total_files=estimated_total_files,
            )
        if scan_eta_seconds is None:
            scan_eta_seconds = _scan_unit_eta_seconds(
                started_at=scan_started_at,
                now=summary_now,
                completed_units=completed_scan_units,
                total_units=total_scan_units,
            )
    manifest_integrity_summary = _manifest_freeze_integrity_summary(manifest)
    worker_version_summary = _job_worker_version_summary(session, job)

    return JobSummaryResponse(
        id=job.id,
        input_dir=job.input_dir,
        output_dir=job.output_dir,
        engine=job.engine,
        assigned_server_id=public_assigned_server_id(job),
        allowed_server_ids=allowed_server_ids_for_job(job),
        status=job.status,
        lifecycle_stage=lifecycle_stage,
        failure_category=job.failure_category,
        error_message=job.error_message,
        stop_requested=job.stop_requested,
        force_reprocess=job.force_reprocess,
        archived_at=job.archived_at,
        total_files=total_files,
        scanned_files=scanned_files,
        completed_files=completed_files,
        failed_files=failed_files,
        failure_category_counts=failure_category_counts,
        skipped_files=skipped_files,
        total_pages=total_pages,
        completed_pages=completed_pages,
        progress_percent=progress_percent,
        pages_per_second=pages_per_second,
        files_per_minute=files_per_minute,
        eta_seconds=eta_seconds,
        last_event_at=last_event_at,
        last_heartbeat_at=last_heartbeat_at,
        is_stale=is_stale,
        degraded_pages=degraded_pages,
        manifest_status=manifest.status if manifest is not None else None,
        manifest_snapshot_status=_manifest_snapshot_status(manifest),
        manifest_frozen_at=manifest.frozen_at if manifest is not None else None,
        **manifest_integrity_summary,
        scan_status=scan_status,
        scan_progress_files=scan_progress_files,
        scan_discovered_pdf_count=scan_progress_files,
        scan_estimated_total_files=estimated_total_files,
        scan_estimated_total_pdf_count=estimated_total_files,
        scan_remaining_files=scan_remaining_files,
        scan_remaining_pdf_count=scan_remaining_files,
        scan_progress_percent=scan_progress_percent,
        scan_progress_dirs=scan_progress_dirs,
        scan_progress_bytes=scan_progress_bytes,
        scan_current_path=scan_progress.get("current_path"),
        scan_error_count=scan_error_count,
        scan_error_samples=scan_error_samples,
        scan_eta_seconds=scan_eta_seconds,
        scan_started_at=scan_started_at,
        scan_finished_at=scan_finished_at,
        total_shards=total_shards,
        shards_created=total_shards,
        executable_shards=executable_shards,
        pending_shards=shard_counts.get("pending", 0),
        running_shards=shard_counts.get("running", 0),
        retrying_shards=retrying_shards,
        stale_shards=stale_shards,
        succeeded_shards=shard_counts.get("succeeded", 0),
        failed_shards=failed_shards,
        stopped_shards=stopped_shards,
        shard_failure_category_counts=shard_failure_category_counts,
        total_scan_units=total_scan_units,
        pending_scan_units=pending_scan_units,
        running_scan_units=running_scan_units,
        stale_scan_units=stale_scan_units,
        succeeded_scan_units=succeeded_scan_units,
        failed_scan_units=failed_scan_units,
        scan_unit_failure_category_counts=scan_unit_failure_category_counts,
        recovery_status=recovery_status,
        **worker_version_summary,
        worker_shards=worker_shards,
        attention_shards=attention_shards,
        quality_flags=quality_flags,
    )


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


def _append_manifest_integrity_issue_sample(samples: list[Any], issue: Any) -> None:
    if MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT == 0:
        return
    if len(samples) < MANIFEST_INTEGRITY_ISSUE_SAMPLE_LIMIT:
        samples.append(issue)


def _scan_unit_status_counts(session: Session, job_id: str) -> dict[str, int]:
    rows = session.execute(
        select(ScanUnit.status, func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job_id)
        .group_by(ScanUnit.status)
    ).all()
    return {status: int(count) for status, count in rows}


def _shard_status_counts(session: Session, job_id: str) -> dict[str, int]:
    rows = session.execute(
        select(WorkShard.status, func.count(WorkShard.id))
        .where(WorkShard.job_id == job_id)
        .group_by(WorkShard.status)
    ).all()
    return {status: int(count) for status, count in rows}


def _manifest_integrity_issue_samples(
    report: ManifestIntegrityResponse,
    *,
    limit: int = 5,
) -> list[dict[str, Any]]:
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


def _manifest_integrity_freeze_summary(report: ManifestIntegrityResponse) -> dict[str, Any]:
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
        "integrity_issue_samples": _manifest_integrity_issue_samples(report),
    }


def _build_manifest_freeze_report(session: Session, job: Job, manifest: Manifest) -> dict[str, Any]:
    scan_unit_counts = _scan_unit_status_counts(session, job.id)
    shard_counts = _shard_status_counts(session, job.id)
    scan_progress = _latest_manifest_scan_progress(session, job.id)
    progress_scan_error_count = int(scan_progress.get("skipped_error_count") or 0)
    progress_scan_error_samples = _recent_manifest_scan_error_samples(session, job.id, limit=10)
    manifest_scan_meta = _manifest_scan_metadata(manifest)
    try:
        manifest_scan_error_count = int(manifest_scan_meta.get("skipped_error_count") or 0)
    except (TypeError, ValueError):
        manifest_scan_error_count = 0
    manifest_scan_error_samples = _manifest_scan_error_samples(manifest_scan_meta, limit=10)
    scan_unit_problem_samples = _scan_unit_problem_samples(session, job.id, limit=10)
    total_scan_units = sum(scan_unit_counts.values())
    total_shards = sum(shard_counts.values())
    shard_file_count = int(
        session.execute(
            select(func.coalesce(func.sum(WorkShard.file_count), 0)).where(
                WorkShard.job_id == job.id
            )
        ).scalar_one()
        or 0
    )
    manifest_file_count = int(manifest.file_count or 0)
    scan_error_samples = (
        progress_scan_error_samples
        or manifest_scan_error_samples
        or scan_unit_problem_samples
    )
    scan_error_count = max(
        progress_scan_error_count,
        manifest_scan_error_count,
        scan_unit_counts.get("failed", 0),
        scan_unit_counts.get("stale", 0),
        len(scan_error_samples),
    )
    integrity_summary = _manifest_integrity_freeze_summary(
        get_manifest_integrity_report(session, job.id)
    )
    return {
        "frozen": manifest.frozen_at is not None,
        "job_id": job.id,
        "manifest_id": manifest.id,
        "input_mode": manifest.input_mode,
        "input_root": manifest.input_root,
        "manifest_path": manifest.manifest_path,
        "meta_path": manifest.meta_path,
        "file_count": manifest_file_count,
        "total_bytes": int(manifest.total_bytes or 0),
        "shard_count": total_shards,
        "shard_file_count": shard_file_count,
        "shard_file_count_matches_manifest": shard_file_count == manifest_file_count,
        "scan_unit_count": total_scan_units,
        "scan_units": {
            "pending": scan_unit_counts.get("pending", 0),
            "running": scan_unit_counts.get("running", 0),
            "stale": scan_unit_counts.get("stale", 0),
            "succeeded": scan_unit_counts.get("succeeded", 0),
            "failed": scan_unit_counts.get("failed", 0),
        },
        "shards": {
            "pending": shard_counts.get("pending", 0),
            "running": shard_counts.get("running", 0),
            "retrying": shard_counts.get("retrying", 0),
            "stale": shard_counts.get("stale", 0),
            "succeeded": shard_counts.get("succeeded", 0),
            "failed": shard_counts.get("failed", 0),
            "stopped": shard_counts.get("stopped", 0),
        },
        "scan_error_count": scan_error_count,
        "scan_error_samples": scan_error_samples,
        "created_at": utcnow().isoformat(),
        **integrity_summary,
    }


def freeze_manifest_if_scan_complete(session: Session, job: Job, manifest: Manifest) -> None:
    active_units = session.execute(
        select(func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status.in_({"pending", "running", "stale"}))
    ).scalar_one()
    failed_units = session.execute(
        select(func.count(ScanUnit.id))
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status == "failed")
    ).scalar_one()
    if int(active_units or 0) != 0 or int(failed_units or 0) != 0:
        return
    manifest.status = "ready"
    if manifest.frozen_at is None:
        manifest.frozen_at = utcnow()
        report = _build_manifest_freeze_report(session, job, manifest)
        report["frozen"] = True
        report["frozen_at"] = manifest.frozen_at.isoformat()
        manifest.freeze_report_json = json_dumps(report)


def get_manifest_freeze_report(session: Session, job_id: str) -> ManifestFreezeReportResponse:
    job = get_job_or_raise(session, job_id)
    manifest = session.execute(
        select(Manifest)
        .where(Manifest.job_id == job_id)
        .order_by(Manifest.id.asc())
        .limit(1)
    ).scalar_one_or_none()
    if manifest is None:
        return ManifestFreezeReportResponse(
            job_id=job_id,
            manifest_id=None,
            status="missing_manifest",
            frozen_at=None,
            report={"frozen": False},
        )
    if manifest.frozen_at is not None:
        return ManifestFreezeReportResponse(
            job_id=job_id,
            manifest_id=manifest.id,
            status=manifest.status,
            frozen_at=manifest.frozen_at,
            report=json_loads_object(manifest.freeze_report_json),
        )
    return ManifestFreezeReportResponse(
        job_id=job_id,
        manifest_id=manifest.id,
        status=manifest.status,
        frozen_at=None,
        report=_build_manifest_freeze_report(session, job, manifest) | {"frozen": False},
    )


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
            if _path_is_under(str(item["path"]), path):
                return True
    return False


def _server_can_read_path(server: Server, path: str | None) -> bool:
    if not path:
        return False
    access = evaluate_server_path_access(server, path)
    return bool(access.get("can_access"))


def _manifest_worker_report_payload(report: ManifestIntegrityResponse) -> dict[str, Any]:
    if hasattr(report, "model_dump"):
        return report.model_dump(mode="json")
    return report.dict()


def _load_worker_integrity_report(manifest: Manifest) -> ManifestIntegrityResponse | None:
    if not manifest.worker_integrity_report_json:
        return None
    try:
        payload = json_loads_object(manifest.worker_integrity_report_json)
    except json.JSONDecodeError:
        return None
    if not payload:
        return None
    payload = dict(payload)
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
    manifest.worker_integrity_status = "pending"
    manifest.worker_integrity_requested_at = now
    manifest.worker_integrity_started_at = None
    manifest.worker_integrity_finished_at = None
    manifest.worker_integrity_server_id = None
    manifest.worker_integrity_report_json = "{}"
    session.commit()
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
        manifest.worker_integrity_status = "running"
        manifest.worker_integrity_started_at = now
        manifest.worker_integrity_server_id = server_id
        session.commit()
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
) -> ManifestIntegrityWorkerRequestResponse:
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
    manifest.worker_integrity_status = "ok" if report.ok else "failed"
    manifest.worker_integrity_finished_at = now
    manifest.worker_integrity_server_id = server_id
    manifest.worker_integrity_report_json = json.dumps(
        _manifest_worker_report_payload(report),
        ensure_ascii=False,
        default=str,
    )
    session.commit()
    return ManifestIntegrityWorkerRequestResponse(
        job_id=manifest.job_id,
        manifest_id=manifest.id,
        worker_integrity_status=manifest.worker_integrity_status or "unknown",
        requested_at=manifest.worker_integrity_requested_at,
    )


def get_manifest_integrity_report(session: Session, job_id: str) -> ManifestIntegrityResponse:
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
        worker_report = _load_worker_integrity_report(manifest)
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
                _append_manifest_integrity_issue_sample(
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
                    _append_manifest_integrity_issue_sample(
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
                        _append_manifest_integrity_issue_sample(
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
                            _append_manifest_integrity_issue_sample(
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
                        _append_manifest_integrity_issue_sample(
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
                            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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
            _append_manifest_integrity_issue_sample(
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


def list_work_shards(
    session: Session,
    job_id: str,
    *,
    status: str = "all",
    worker_id: str | None = None,
    failure_category: str | None = None,
    min_attempt_count: int | None = None,
    running_longer_than_seconds: int | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[WorkShard], int]:
    get_job_or_raise(session, job_id)
    status_filter = _normalized_shard_status_filter(status)
    filters = [WorkShard.job_id == job_id]
    if status_filter == "attention":
        filters.append(WorkShard.status.in_(ATTENTION_SHARD_STATUSES))
    elif status_filter != "all":
        filters.append(WorkShard.status == status_filter)
    if worker_id:
        filters.append(WorkShard.assigned_server_id == worker_id)
    if failure_category:
        filters.append(WorkShard.failure_category == failure_category)
    if min_attempt_count is not None:
        filters.append(WorkShard.attempt_count >= min_attempt_count)
    if running_longer_than_seconds is not None:
        threshold = utcnow() - timedelta(seconds=running_longer_than_seconds)
        filters.append(WorkShard.status == "running")
        filters.append(WorkShard.started_at.is_not(None))
        filters.append(WorkShard.started_at <= threshold)
    total = int(
        session.execute(
            select(func.count(WorkShard.id)).where(*filters)
        ).scalar_one()
        or 0
    )
    stmt = (
        select(WorkShard)
        .where(*filters)
        .order_by(WorkShard.shard_index.asc())
        .offset(max(offset, 0))
        .limit(max(limit, 1))
    )
    return list(session.execute(stmt).scalars().all()), total


def has_static_shards(session: Session, job_id: str) -> bool:
    return bool(
        session.execute(
            select(func.count(WorkShard.id)).where(WorkShard.job_id == job_id)
        ).scalar_one()
    )


def stop_reclaimable_work_for_job(session: Session, job: Job) -> None:
    current_time = utcnow()
    session.execute(
        update(WorkShard)
        .where(WorkShard.job_id == job.id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
        .values(
            status="stopped",
            failure_category="operator_stopped",
            lease_expires_at=None,
            finished_at=current_time,
        )
    )
    session.execute(
        update(ScanUnit)
        .where(ScanUnit.job_id == job.id)
        .where(ScanUnit.status.in_(RECLAIMABLE_SCAN_UNIT_STATUSES))
        .values(
            status="stopped",
            failure_category="operator_stopped",
            lease_expires_at=None,
            finished_at=current_time,
        )
    )


def finalize_stopped_job_if_idle(session: Session, job: Job) -> bool:
    if not job.stop_requested or job.status in TERMINAL_JOB_STATUSES:
        return False
    total_shards = int(
        session.execute(
            select(func.count(WorkShard.id)).where(WorkShard.job_id == job.id)
        ).scalar_one()
        or 0
    )
    total_scan_units = int(
        session.execute(
            select(func.count(ScanUnit.id)).where(ScanUnit.job_id == job.id)
        ).scalar_one()
        or 0
    )
    if total_shards == 0 and total_scan_units == 0:
        return False
    open_shards = int(
        session.execute(
            select(func.count(WorkShard.id))
            .where(WorkShard.job_id == job.id)
            .where(WorkShard.status.not_in(TERMINAL_SHARD_STATUSES))
        ).scalar_one()
        or 0
    )
    open_scan_units = int(
        session.execute(
            select(func.count(ScanUnit.id))
            .where(ScanUnit.job_id == job.id)
            .where(ScanUnit.status.in_({"pending", "running", "stale"}))
        ).scalar_one()
        or 0
    )
    if open_shards or open_scan_units:
        return False
    job.status = "stopped"
    if job.failure_category is None:
        job.failure_category = "operator_stopped"
    if job.finished_at is None:
        job.finished_at = utcnow()
    return True


def _claimable_shard_id_select(job_id: str):
    return (
        select(WorkShard.id)
        .where(WorkShard.job_id == job_id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
        .order_by(WorkShard.shard_index.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )


def _create_shard_attempt(session: Session, shard: WorkShard, server_id: str) -> None:
    session.add(
        ShardAttempt(
            job_id=shard.job_id,
            shard_id=shard.id,
            attempt_number=shard.attempt_count,
            server_id=server_id,
            status="running",
            processed_files=shard.processed_files,
            failed_files=shard.failed_files,
            skipped_files=shard.skipped_files,
            completed_pages=shard.completed_pages,
            execution_paused=shard.execution_paused,
            api_concurrency_limit=shard.api_concurrency_limit,
            execution_control_reason=shard.execution_control_reason,
            started_at=shard.started_at or utcnow(),
        )
    )


def _latest_shard_attempt(session: Session, shard: WorkShard) -> ShardAttempt | None:
    return session.execute(
        select(ShardAttempt)
        .where(ShardAttempt.shard_id == shard.id)
        .where(ShardAttempt.attempt_number == shard.attempt_count)
        .order_by(ShardAttempt.id.desc())
        .limit(1)
    ).scalar_one_or_none()


def claim_next_pending_shard(session: Session, job_id: str, server_id: str) -> WorkShard | None:
    job = get_job_or_raise(session, job_id)
    non_claimable_statuses = {"stopping", *TERMINAL_JOB_STATUSES}
    if job.stop_requested or job.status in non_claimable_statuses:
        return None
    if job.assigned_server_id == POOL_SERVER_ID and not server_can_access_input_dir(
        session,
        server_id,
        job.input_dir,
    ):
        return None
    if job.assigned_server_id == POOL_SERVER_ID and not server_is_allowed_for_job(
        job,
        server_id,
    ):
        return None

    now = utcnow()
    reconcile_expired_shard_leases(session, now=now, job_id=job_id)
    shard_id = session.execute(_claimable_shard_id_select(job_id)).scalar_one_or_none()
    if shard_id is None:
        return None

    started_at = now
    lease_expires_at = shard_lease_deadline(now)
    claimable_parent = (
        select(Job.id)
        .where(Job.id == job_id)
        .where(Job.stop_requested.is_(False))
        .where(Job.status.not_in(non_claimable_statuses))
        .exists()
    )
    claim_stmt = (
        update(WorkShard)
        .where(WorkShard.id == shard_id)
        .where(WorkShard.status.in_(RECLAIMABLE_SHARD_STATUSES))
        .where(claimable_parent)
        .values(
            status="running",
            assigned_server_id=server_id,
            failure_category=None,
            error_message=None,
            attempt_count=WorkShard.attempt_count + 1,
            started_at=started_at,
            finished_at=None,
            lease_expires_at=lease_expires_at,
        )
    )
    result = session.execute(claim_stmt)
    if result.rowcount != 1:
        session.rollback()
        parent_claimable = session.execute(select(claimable_parent)).scalar_one()
        if parent_claimable:
            return claim_next_pending_shard(session, job_id, server_id)
        return None

    shard = session.get(WorkShard, shard_id)
    if shard is not None:
        session.refresh(shard)
        _create_shard_attempt(session, shard, server_id)
    session.commit()
    return session.get(WorkShard, shard_id)


def update_work_shard(session: Session, shard_id: int, request: WorkShardUpdateRequest) -> WorkShard:
    shard = session.execute(
        select(WorkShard)
        .where(WorkShard.id == shard_id)
        .with_for_update()
    ).scalar_one_or_none()
    if shard is None:
        raise ValueError(f"unknown shard: {shard_id}")
    if request.assigned_server_id is not None and request.assigned_server_id != shard.assigned_server_id:
        raise ShardAttemptConflictError(
            "shard update belongs to a different server attempt"
        )
    if request.attempt_count is not None and request.attempt_count != shard.attempt_count:
        raise ShardAttemptConflictError(
            "shard update belongs to a stale attempt"
        )
    job = get_job_or_raise(session, shard.job_id)
    fields_set = getattr(request, "model_fields_set", None)
    if fields_set is None:
        fields_set = getattr(request, "__fields_set__", set())

    shard.status = request.status
    if "processed_files" in fields_set:
        shard.processed_files = request.processed_files
    if "failed_files" in fields_set:
        shard.failed_files = request.failed_files
    if "skipped_files" in fields_set:
        shard.skipped_files = request.skipped_files
    if "completed_pages" in fields_set:
        shard.completed_pages = request.completed_pages
    if request.api_inflight is not None:
        shard.api_inflight = request.api_inflight
    if request.api_inflight_peak is not None:
        shard.api_inflight_peak = request.api_inflight_peak
    if request.api_waiting is not None:
        shard.api_waiting = request.api_waiting
    if request.oldest_api_inflight is not None:
        shard.oldest_api_inflight = request.oldest_api_inflight
    if request.execution_paused is not None:
        shard.execution_paused = request.execution_paused
    if request.api_concurrency_limit is not None:
        shard.api_concurrency_limit = request.api_concurrency_limit
    if request.execution_control_reason is not None:
        shard.execution_control_reason = request.execution_control_reason
    failure_category = request.failure_category
    if failure_category is None and request.status == "failed":
        failure_category = infer_failure_category(
            {"error_message": request.error_message}
        )
    shard.failure_category = failure_category
    shard.error_message = request.error_message
    if request.status == "failed":
        shard.status = _remaining_retry_status(job, shard)
    if shard.status in TERMINAL_SHARD_STATUSES:
        shard.finished_at = utcnow()
        shard.lease_expires_at = None
    elif shard.status in {"retrying", "stale"}:
        shard.finished_at = None
        shard.lease_expires_at = None
    attempt = _latest_shard_attempt(session, shard)
    if attempt is not None:
        attempt.status = shard.status
        attempt.processed_files = shard.processed_files
        attempt.failed_files = shard.failed_files
        attempt.skipped_files = shard.skipped_files
        attempt.completed_pages = shard.completed_pages
        attempt.execution_paused = shard.execution_paused
        attempt.api_concurrency_limit = shard.api_concurrency_limit
        attempt.execution_control_reason = shard.execution_control_reason
        attempt.failure_category = failure_category
        attempt.error_message = request.error_message
        if shard.status != "running":
            attempt.finished_at = utcnow()
    if shard.status == "failed":
        open_shards = session.execute(
            select(func.count(WorkShard.id))
            .where(WorkShard.job_id == shard.job_id)
            .where(WorkShard.id != shard.id)
            .where(WorkShard.status.not_in(TERMINAL_SHARD_STATUSES))
        ).scalar_one()
        if not open_shards and job.status not in TERMINAL_JOB_STATUSES:
            job.status = "failed"
            job.failure_category = shard.failure_category
            job.error_message = shard.error_message
            job.finished_at = utcnow()
    finalize_stopped_job_if_idle(session, job)
    session.commit()
    session.refresh(shard)
    return shard


def list_shard_attempts(
    session: Session,
    job_id: str,
    shard_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> list[ShardAttempt]:
    get_job_or_raise(session, job_id)
    shard = session.get(WorkShard, shard_id)
    if shard is None or shard.job_id != job_id:
        raise ValueError(f"unknown shard for job: {shard_id}")
    return list(
        session.execute(
            select(ShardAttempt)
            .where(ShardAttempt.shard_id == shard_id)
            .order_by(ShardAttempt.attempt_number.asc(), ShardAttempt.id.asc())
            .offset(max(offset, 0))
            .limit(max(limit, 1))
        ).scalars().all()
    )


def list_shard_attempts_page(
    session: Session,
    job_id: str,
    shard_id: int,
    *,
    limit: int = 100,
    offset: int = 0,
) -> ShardAttemptListResponse:
    attempts = list_shard_attempts(
        session,
        job_id,
        shard_id,
        limit=limit,
        offset=offset,
    )
    total = int(
        session.execute(
            select(func.count(ShardAttempt.id)).where(ShardAttempt.shard_id == shard_id)
        ).scalar_one()
        or 0
    )
    bounded_offset = max(offset, 0)
    bounded_limit = max(limit, 1)
    return ShardAttemptListResponse(
        job_id=job_id,
        shard_id=shard_id,
        total=total,
        limit=bounded_limit,
        offset=bounded_offset,
        has_more=bounded_offset + len(attempts) < total,
        items=[shard_attempt_to_response(attempt) for attempt in attempts],
    )


def shard_attempt_to_response(attempt: ShardAttempt) -> ShardAttemptResponse:
    return ShardAttemptResponse(
        id=attempt.id,
        job_id=attempt.job_id,
        shard_id=attempt.shard_id,
        attempt_number=attempt.attempt_number,
        server_id=attempt.server_id,
        status=attempt.status,
        processed_files=attempt.processed_files,
        failed_files=attempt.failed_files,
        skipped_files=attempt.skipped_files,
        completed_pages=attempt.completed_pages,
        execution_paused=attempt.execution_paused,
        api_concurrency_limit=attempt.api_concurrency_limit,
        execution_control_reason=attempt.execution_control_reason,
        failure_category=attempt.failure_category,
        error_message=attempt.error_message,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
    )


def list_recent_job_files(session: Session, job_id: str, kind: str, limit: int) -> list[JobFile]:
    get_job_or_raise(session, job_id)
    bounded_limit = max(1, min(limit, 100))
    stmt = select(JobFile).where(JobFile.job_id == job_id)
    if kind == "failed":
        stmt = stmt.where(JobFile.status.in_(FAILED_FILE_STATUSES))
    elif kind == "processed":
        stmt = stmt.where(JobFile.status.in_(PROCESSED_FILE_STATUSES))
    else:
        stmt = stmt.where(JobFile.status.in_(PROCESSED_FILE_STATUSES))
    stmt = stmt.order_by(JobFile.updated_at.desc(), JobFile.id.desc()).limit(bounded_limit)
    rows = list(session.execute(stmt).scalars().all())
    if kind != "failed" or len(rows) >= bounded_limit:
        return rows

    seen_paths = {row.file_path for row in rows}
    counter = session.get(JobCounter, job_id)
    for sample in _load_recent_failed_file_samples(counter):
        file_path = str(sample.get("file_path") or "")
        if not file_path or file_path in seen_paths:
            continue
        rows.append(
            JobFile(
                job_id=job_id,
                file_path=file_path,
                filename=str(sample.get("filename") or file_path.rsplit("/", 1)[-1]),
                status="failed",
                total_pages=_optional_int(sample.get("total_pages")),
                done_pages=_optional_int(sample.get("done_pages")) or 0,
                output_path=sample.get("output_path"),
                error=sample.get("error"),
                failure_category=sample.get("failure_category"),
            )
        )
        seen_paths.add(file_path)
        if len(rows) >= bounded_limit:
            break
    return rows


def _recent_error_from_event(row: JobEvent) -> JobRecentErrorResponse:
    payload = json_loads_object(row.payload_json)
    failure_category = row.failure_category or payload.get("failure_category") or infer_failure_category(payload)
    return JobRecentErrorResponse(
        source="job_event",
        event_type=row.event_type,
        file_path=row.file_path or payload.get("file_path"),
        filename=payload.get("filename"),
        failure_category=str(failure_category) if failure_category else None,
        error=payload.get("error") or payload.get("error_message"),
        created_at=row.created_at,
        payload=payload,
    )


def _recent_error_from_failed_file_sample(sample: dict[str, Any]) -> JobRecentErrorResponse:
    return JobRecentErrorResponse(
        source="failed_file_sample",
        event_type="file_failed",
        file_path=sample.get("file_path"),
        filename=sample.get("filename"),
        failure_category=sample.get("failure_category"),
        error=sample.get("error"),
        created_at=None,
        payload=dict(sample),
    )


def _recent_error_from_event_sample(sample: dict[str, Any]) -> JobRecentErrorResponse:
    payload = sample.get("payload")
    return JobRecentErrorResponse(
        source="event_sample",
        event_type=sample.get("event_type"),
        file_path=sample.get("file_path"),
        filename=sample.get("filename"),
        failure_category=sample.get("failure_category"),
        error=sample.get("error"),
        created_at=_parse_datetime(sample.get("created_at")),
        payload=payload if isinstance(payload, dict) else dict(sample),
    )


def list_recent_job_errors_page(
    session: Session,
    job_id: str,
    *,
    limit: int,
    offset: int,
    failure_category: str | None = None,
) -> JobRecentErrorListResponse:
    get_job_or_raise(session, job_id)
    filters = [JobEvent.job_id == job_id, JobEvent.event_type.in_(PRIORITY_FAILURE_EVENT_TYPES)]
    if failure_category:
        filters.append(JobEvent.failure_category == failure_category)
        total = int(session.execute(select(func.count(JobEvent.id)).where(*filters)).scalar_one())
        event_rows = list(
            session.execute(
                select(JobEvent)
                .where(*filters)
                .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
                .offset(offset)
                .limit(limit)
            ).scalars()
        )
        items = [_recent_error_from_event(row) for row in event_rows]
    else:
        total = int(session.execute(select(func.count(JobEvent.id)).where(*filters)).scalar_one())
        event_rows = list(
            session.execute(
                select(JobEvent)
                .where(*filters)
                .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
                .offset(offset)
                .limit(limit)
            ).scalars()
        )
        items = [_recent_error_from_event(row) for row in event_rows]
    if total == 0:
        event_samples = [
            _recent_error_from_event_sample(sample)
            for sample in _load_recent_error_samples(session.get(JobCounter, job_id))
        ]
        if failure_category:
            event_samples = [
                item for item in event_samples if item.failure_category == failure_category
            ]
        samples = [
            _recent_error_from_failed_file_sample(sample)
            for sample in _load_recent_failed_file_samples(session.get(JobCounter, job_id))
        ]
        if failure_category:
            samples = [
                item for item in samples if item.failure_category == failure_category
            ]
        fallback_items = event_samples + samples
        total = len(fallback_items)
        items = fallback_items[offset : offset + limit]
    return JobRecentErrorListResponse(
        job_id=job_id,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(items) < total,
        items=items,
    )


def request_stop(session: Session, job_id: str) -> Job:
    job = get_job_or_raise(session, job_id)
    job.stop_requested = True
    if job.status == "queued":
        job.status = "stopped"
        if job.finished_at is None:
            job.finished_at = utcnow()
    elif job.status == "running":
        job.status = "stopping"
    stop_reclaimable_work_for_job(session, job)
    finalize_stopped_job_if_idle(session, job)
    session.commit()
    session.refresh(job)
    return job


def delete_job(session: Session, job_id: str) -> None:
    job = get_job_or_raise(session, job_id)
    if job.status not in TERMINAL_JOB_STATUSES:
        raise JobNotTerminalError(f"job is not terminal: {job_id}")

    session.delete(job)
    session.commit()


def archive_job(session: Session, job_id: str) -> Job:
    job = get_job_or_raise(session, job_id)
    if job.status not in TERMINAL_JOB_STATUSES:
        raise JobNotTerminalError(f"job is not terminal: {job_id}")
    if job.archived_at is None:
        job.archived_at = utcnow()
        session.commit()
        session.refresh(job)
    return job


def get_job_or_raise(session: Session, job_id: str) -> Job:
    job = session.get(Job, job_id)
    if job is None:
        raise UnknownJobError(f"unknown job: {job_id}")
    return job


def parse_page_no(payload: dict[str, Any]) -> int | None:
    page_no = payload.get("page_no")
    if page_no is None:
        return None
    return int(page_no)


def upsert_job_file_from_event(session: Session, job: Job, event: JobEventRequest) -> None:
    payload = event.payload
    file_path = payload.get("file_path")
    if not file_path:
        return

    filename = payload.get("filename") or file_path.rsplit("/", 1)[-1]
    stmt = select(JobFile).where(JobFile.job_id == job.id).where(JobFile.file_path == file_path)
    job_file = session.execute(stmt).scalar_one_or_none()
    if job_file is None:
        job_file = JobFile(
            job_id=job.id,
            file_path=file_path,
            filename=filename,
            status="pending",
            done_pages=0,
        )
        session.add(job_file)

    if event.type == "file_started":
        job_file.status = "running"
        job_file.error = None
        job_file.failure_category = None
        if payload.get("total_pages") is not None:
            job_file.total_pages = int(payload["total_pages"])
    elif event.type == "page_done":
        page_no = parse_page_no(payload)
        if page_no is not None:
            done_pages_stmt = (
                select(func.count(distinct(JobEvent.page_no)))
                .where(JobEvent.job_id == job.id)
                .where(JobEvent.file_path == file_path)
                .where(JobEvent.event_type == "page_done")
                .where(JobEvent.page_no.is_not(None))
            )
            job_file.done_pages = int(session.execute(done_pages_stmt).scalar_one())
        job_file.status = "running"
        if payload.get("status") in {"error", "failed"}:
            job_file.status = "failed"
            job_file.error = payload.get("error")
            job_file.failure_category = infer_failure_category(payload)
    elif event.type == "file_done":
        job_file.status = payload.get("status") or "success"
        job_file.output_path = payload.get("output_path")
        job_file.error = None
        job_file.failure_category = None
    elif event.type == "file_failed":
        job_file.status = "failed"
        job_file.error = payload.get("error")
        job_file.failure_category = infer_failure_category(payload)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def get_or_create_job_counter(session: Session, job_id: str) -> JobCounter:
    counter = session.get(JobCounter, job_id)
    if counter is None:
        counter = JobCounter(job_id=job_id)
        session.add(counter)
        session.flush()
    return counter


def _load_recent_failed_file_samples(counter: JobCounter | None) -> list[dict[str, Any]]:
    if counter is None:
        return []
    try:
        value = json.loads(counter.recent_failed_files_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    samples: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        file_path = str(item.get("file_path") or "")
        if not file_path:
            continue
        samples.append(item)
    return samples


def _load_recent_error_samples(counter: JobCounter | None) -> list[dict[str, Any]]:
    if counter is None:
        return []
    try:
        value = json.loads(counter.recent_errors_json or "[]")
    except json.JSONDecodeError:
        return []
    if not isinstance(value, list):
        return []
    samples: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        if not item.get("event_type"):
            continue
        samples.append(item)
    return samples


def _load_failure_category_counts(counter: JobCounter | None) -> dict[str, int]:
    if counter is None:
        return {}
    try:
        value = json.loads(counter.failure_category_counts_json or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    counts: dict[str, int] = {}
    for key, count in value.items():
        try:
            numeric_count = int(count)
        except (TypeError, ValueError):
            continue
        if numeric_count > 0:
            counts[str(key)] = numeric_count
    return counts


def _increment_failure_category_count(counter: JobCounter, category: str) -> None:
    counts = _load_failure_category_counts(counter)
    counts[category] = counts.get(category, 0) + 1
    counter.failure_category_counts_json = json_dumps(
        dict(sorted(counts.items(), key=lambda item: item[0]))
    )


def _failed_file_sample_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    file_path = payload.get("file_path")
    if not file_path:
        return None
    file_path = str(file_path)
    filename = payload.get("filename") or file_path.rsplit("/", 1)[-1]
    return {
        "file_path": file_path,
        "filename": str(filename),
        "status": "failed",
        "total_pages": _optional_int(payload.get("total_pages")),
        "done_pages": _optional_int(payload.get("done_pages")) or 0,
        "output_path": payload.get("output_path"),
        "error": payload.get("error"),
        "failure_category": infer_failure_category(payload),
    }


def _store_recent_failed_file_sample(counter: JobCounter, payload: dict[str, Any]) -> None:
    limit = max(0, JOB_FAILED_FILE_SAMPLE_LIMIT)
    sample = _failed_file_sample_from_payload(payload)
    if sample is None:
        return
    if limit == 0:
        counter.recent_failed_files_json = "[]"
        return

    file_path = sample["file_path"]
    samples = [
        item
        for item in _load_recent_failed_file_samples(counter)
        if str(item.get("file_path") or "") != file_path
    ]
    samples.insert(0, sample)
    counter.recent_failed_files_json = json_dumps(samples[:limit])


def _failure_event_sample_from_event(
    event: JobEventRequest,
    *,
    event_time: datetime,
) -> dict[str, Any] | None:
    if event.type not in PRIORITY_FAILURE_EVENT_TYPES or event.type == "file_failed":
        return None
    payload = event.payload
    return {
        "event_type": event.type,
        "file_path": payload.get("file_path"),
        "filename": payload.get("filename"),
        "failure_category": infer_failure_category(payload),
        "error": payload.get("error") or payload.get("error_message"),
        "created_at": event_time.isoformat(),
        "payload": payload,
    }


def _store_recent_error_sample(
    counter: JobCounter,
    event: JobEventRequest,
    *,
    event_time: datetime,
) -> None:
    limit = max(0, JOB_RECENT_ERROR_SAMPLE_LIMIT)
    sample = _failure_event_sample_from_event(event, event_time=event_time)
    if sample is None:
        return
    if limit == 0:
        counter.recent_errors_json = "[]"
        return

    samples = _load_recent_error_samples(counter)
    samples.insert(0, sample)
    counter.recent_errors_json = json_dumps(samples[:limit])


def job_counter_event_already_seen(session: Session, job_id: str, event: JobEventRequest) -> bool:
    payload = event.payload
    file_path = payload.get("file_path")
    if not file_path:
        return False
    stmt = (
        select(JobEvent.id)
        .where(JobEvent.job_id == job_id)
        .where(JobEvent.event_type == event.type)
        .where(JobEvent.file_path == file_path)
    )
    if event.type == "page_done":
        page_no = parse_page_no(payload)
        if page_no is None:
            return False
        stmt = stmt.where(JobEvent.page_no == page_no)
    elif event.type not in {"file_started", "file_done", "file_failed"}:
        return False
    return session.execute(stmt.limit(1)).scalar_one_or_none() is not None


def update_job_counter_from_event(
    session: Session,
    job: Job,
    event: JobEventRequest,
    *,
    event_time: datetime,
) -> JobCounter:
    counter = get_or_create_job_counter(session, job.id)
    if counter.first_event_at is None:
        counter.first_event_at = event_time
    counter.last_event_at = event_time

    payload = event.payload
    if job_counter_event_already_seen(session, job.id, event):
        return counter
    if event.type == "file_started":
        counter.started_files += 1
        if payload.get("total_pages") is not None:
            try:
                counter.total_pages += int(payload["total_pages"])
            except (TypeError, ValueError):
                pass
    elif event.type == "page_done":
        counter.completed_pages += 1
        if payload.get("status") in DEGRADED_PAGE_STATUSES:
            counter.degraded_pages += 1
    elif event.type == "file_done":
        if payload.get("status") == "skipped":
            counter.skipped_files += 1
        else:
            counter.completed_files += 1
    elif event.type == "file_failed":
        counter.failed_files += 1
        _increment_failure_category_count(counter, infer_failure_category(payload))
        _store_recent_failed_file_sample(counter, payload)
    if event.type in PRIORITY_FAILURE_EVENT_TYPES:
        _store_recent_error_sample(counter, event, event_time=event_time)
    return counter


def _job_counter_total_files(counter: JobCounter | None) -> int:
    if counter is None:
        return 0
    terminal_files = counter.completed_files + counter.failed_files + counter.skipped_files
    return max(counter.started_files, terminal_files)


def prune_job_detail_rows(session: Session, job_id: str) -> None:
    if JOB_FILE_DETAIL_LIMIT >= 0:
        if JOB_FILE_DETAIL_LIMIT == 0:
            stale_file_ids = list(
                session.execute(
                    select(JobFile.id).where(JobFile.job_id == job_id)
                ).scalars()
            )
        else:
            recent_file_ids = list(
                session.execute(
                    select(JobFile.id)
                    .where(JobFile.job_id == job_id)
                    .order_by(
                        case((JobFile.status.in_(FAILED_FILE_STATUSES), 0), else_=1),
                        JobFile.updated_at.desc(),
                        JobFile.id.desc(),
                    )
                    .limit(JOB_FILE_DETAIL_LIMIT + 1)
                ).scalars()
            )
            stale_file_ids = recent_file_ids[JOB_FILE_DETAIL_LIMIT:]
        if stale_file_ids:
            session.execute(delete(JobFile).where(JobFile.id.in_(stale_file_ids)))
    if JOB_EVENT_DETAIL_LIMIT >= 0:
        if JOB_EVENT_DETAIL_LIMIT == 0:
            stale_event_ids = list(
                session.execute(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id)
                    .where(
                        JobEvent.event_type.not_in(
                            RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED
                        )
                    )
                ).scalars()
            )
        else:
            recent_event_ids = list(
                session.execute(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id)
                    .order_by(
                        case((JobEvent.event_type.in_(PRIORITY_FAILURE_EVENT_TYPES), 0), else_=1),
                        case((JobEvent.event_type.in_(PRIORITY_TERMINAL_EVENT_TYPES), 0), else_=1),
                        JobEvent.created_at.desc(),
                        JobEvent.id.desc(),
                    )
                    .limit(JOB_EVENT_DETAIL_LIMIT + 1)
                ).scalars()
            )
            stale_event_ids = recent_event_ids[JOB_EVENT_DETAIL_LIMIT:]
        if stale_event_ids:
            session.execute(delete(JobEvent).where(JobEvent.id.in_(stale_event_ids)))
    if JOB_EVENT_DETAIL_LIMIT == 0 and RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED >= 0:
        for event_type in RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED:
            retained_event_ids = list(
                session.execute(
                    select(JobEvent.id)
                    .where(JobEvent.job_id == job_id)
                    .where(JobEvent.event_type == event_type)
                    .order_by(JobEvent.created_at.desc(), JobEvent.id.desc())
                    .limit(RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED + 1)
                ).scalars()
            )
            stale_retained_event_ids = retained_event_ids[
                RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED:
            ]
            if stale_retained_event_ids:
                session.execute(
                    delete(JobEvent).where(JobEvent.id.in_(stale_retained_event_ids))
                )


def record_event(session: Session, job_id: str, event: JobEventRequest) -> Job:
    job = get_job_or_raise(session, job_id)
    payload = event.payload
    page_no = parse_page_no(payload)
    event_time = utcnow()
    update_job_counter_from_event(session, job, event, event_time=event_time)
    if (
        JOB_EVENT_DETAIL_LIMIT != 0
        or event.type in RETAINED_CONTROL_EVENT_TYPES_WHEN_DETAILS_DISABLED
    ):
        failure_category = (
            infer_failure_category(payload)
            if event.type in PRIORITY_FAILURE_EVENT_TYPES
            else None
        )
        row = JobEvent(
            job_id=job.id,
            event_type=event.type,
            file_path=payload.get("file_path"),
            page_no=page_no,
            status=payload.get("status"),
            failure_category=failure_category,
            payload_json=json_dumps(payload),
            created_at=event_time,
        )
        session.add(row)
        session.flush()
    if JOB_FILE_DETAIL_LIMIT != 0:
        upsert_job_file_from_event(session, job, event)
    session.flush()
    prune_job_detail_rows(session, job.id)

    terminal_status = TERMINAL_EVENT_STATUSES.get(event.type)
    is_static_child_terminal = (
        terminal_status is not None
        and has_static_shards(session, job.id)
        and not payload.get("static_shards_final")
    )
    stop_active = job.status == "stopping" or (
        job.stop_requested and job.status not in TERMINAL_JOB_STATUSES
    )
    if terminal_status is not None and not is_static_child_terminal:
        if stop_active:
            job.status = "stopped"
            if job.failure_category is None:
                job.failure_category = "operator_stopped"
            if job.finished_at is None:
                job.finished_at = utcnow()
        elif job.status not in TERMINAL_JOB_STATUSES:
            job.status = terminal_status
            if event.type == "job_failed":
                job.failure_category = infer_failure_category(payload)
                job.error_message = payload.get("error") or payload.get("error_message")
            job.finished_at = utcnow()

    session.commit()
    session.refresh(job)
    return job


def record_log(session: Session, job_id: str, request: JobLogRequest) -> JobLog:
    get_job_or_raise(session, job_id)
    if JOB_LOG_DETAIL_LIMIT == 0:
        session.commit()
        return JobLog(
            job_id=job_id,
            server_id=request.server_id,
            stream=request.stream,
            line=request.line,
        )
    row = JobLog(
        job_id=job_id,
        server_id=request.server_id,
        stream=request.stream,
        line=request.line,
    )
    session.add(row)
    session.flush()
    if JOB_LOG_DETAIL_LIMIT >= 0:
        recent_log_ids = list(
            session.execute(
                select(JobLog.id)
                .where(JobLog.job_id == job_id)
                .order_by(JobLog.created_at.desc(), JobLog.id.desc())
                .limit(JOB_LOG_DETAIL_LIMIT + 1)
            ).scalars()
        )
        stale_log_ids = recent_log_ids[JOB_LOG_DETAIL_LIMIT:]
        if stale_log_ids:
            session.execute(delete(JobLog).where(JobLog.id.in_(stale_log_ids)))
    session.commit()
    session.refresh(row)
    return row


def job_log_to_response(row: JobLog) -> JobLogResponse:
    return JobLogResponse(
        id=row.id,
        job_id=row.job_id,
        server_id=row.server_id,
        stream=row.stream,
        line=row.line,
        created_at=row.created_at,
    )


def list_job_logs_page(
    session: Session,
    job_id: str,
    *,
    limit: int,
    offset: int,
    server_id: str | None = None,
    stream: str | None = None,
) -> JobLogListResponse:
    get_job_or_raise(session, job_id)
    filters = [JobLog.job_id == job_id]
    if server_id:
        filters.append(JobLog.server_id == server_id)
    if stream:
        filters.append(JobLog.stream == stream)
    total = int(
        session.execute(select(func.count(JobLog.id)).where(*filters)).scalar_one()
    )
    rows = list(
        session.execute(
            select(JobLog)
            .where(*filters)
            .order_by(JobLog.created_at.desc(), JobLog.id.desc())
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return JobLogListResponse(
        job_id=job_id,
        total=total,
        limit=limit,
        offset=offset,
        has_more=offset + len(rows) < total,
        items=[job_log_to_response(row) for row in rows],
    )
