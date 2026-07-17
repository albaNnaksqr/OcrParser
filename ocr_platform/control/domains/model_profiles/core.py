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

from ... import database
from ...models import Job, JobCounter, JobEvent, JobFile, JobLog, Manifest, ModelProfile, ScanUnit, Server, ShardAttempt, WorkShard
from ...schemas import (
    JobCreateRequest, JobEventRequest, JobLogListResponse, JobLogRequest, JobLogResponse,
    ManifestFreezeReportResponse, ManifestIntegrityResponse, ManifestIntegrityWorkerCompleteRequest,
    ManifestIntegrityWorkerRequestResponse, ManifestIntegrityWorkerTask, ManifestIntegrityWorkerShardTask,
    ManifestIntegrityScanUnitIssue, ManifestIntegrityShardIssue, JobPreflightIssue, JobPreflightResponse,
    JobRecentErrorListResponse, JobRecentErrorResponse, JobSummaryListResponse, JobShardProgressSummary,
    JobSummaryResponse, JobWorkerShardSummary, ModelProfileRequest, ModelProfileResponse,
    ScanUnitCompleteRequest, ScanUnitFailRequest, ServerHeartbeatRequest, ServerRegisterRequest,
    ShardAttemptListResponse, WorkShardUpdateRequest, RemoteManifestRegisterRequest, ShardAttemptResponse,
)
from ..common import *


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

__all__ = [name for name in globals() if not name.startswith("__")]
