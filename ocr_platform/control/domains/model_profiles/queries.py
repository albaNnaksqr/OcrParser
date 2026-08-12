from __future__ import annotations

import os
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import ModelProfile
from ...schemas import JobCreateRequest
from ...settings import ControlSettings
from ..common import json_loads_object, saved_model_profile_keys_allowed
from . import policy


def list_model_profiles(session: Session) -> list[ModelProfile]:
    return session.execute(
        select(ModelProfile).order_by(ModelProfile.id)
    ).scalars().all()


def get_model_profile_or_raise(
    session: Session,
    profile_id: str,
) -> ModelProfile:
    profile = session.get(ModelProfile, profile_id)
    if profile is None:
        raise ValueError(f"unknown model_profile_id: {profile_id}")
    return profile


def resolve_model_profile_api_key(profile: ModelProfile) -> str | None:
    if profile.api_key:
        return profile.api_key
    if profile.api_key_env_var:
        value = os.environ.get(profile.api_key_env_var)
        return value or None
    return None


def _resolve_job_extra_args_api_key_env_var(
    extra_args: dict[str, Any],
) -> str | None:
    raw_env_var = extra_args.get("api_key_env_var")
    if raw_env_var is None:
        return None
    env_var = str(raw_env_var).strip()
    if not env_var:
        return None
    value = os.environ.get(env_var)
    if not value:
        raise ValueError(
            "job extra_args api_key_env_var is not set in the control "
            f"server environment: {env_var}"
        )
    return value


def _validate_job_extra_args_saved_api_key(
    extra_args: dict[str, Any],
    *,
    settings: ControlSettings | None = None,
) -> None:
    if saved_model_profile_keys_allowed(settings):
        return
    if extra_args.get("api_key"):
        raise ValueError(
            "saved job api_key is disabled; set extra_args.api_key_env_var "
            "on the control server environment instead"
        )


def effective_job_model_config(
    session: Session,
    request: JobCreateRequest,
    *,
    settings: ControlSettings | None = None,
) -> dict[str, Any]:
    policy.reject_secret_like_extra_args(
        request.extra_args,
        context="job",
        allowed_names={"api_key", "api_key_env_var"},
    )
    request_extra_args = policy.normalize_parser_extra_args(
        request.extra_args,
        context="job",
    )
    _validate_job_extra_args_saved_api_key(
        request_extra_args,
        settings=settings,
    )
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
    profile_extra_args = policy.normalize_parser_extra_args(
        json_loads_object(profile.extra_args_json),
        context="model profile",
    )
    extra_args = policy.normalize_parser_extra_args(
        {**profile_extra_args, **request_extra_args},
        context="job",
    )
    job_api_key_from_env = _resolve_job_extra_args_api_key_env_var(extra_args)
    has_api_key = bool(
        extra_args.get("api_key")
        or job_api_key_from_env
        or resolve_model_profile_api_key(profile)
    )
    if profile.requires_api_key and not has_api_key:
        raise ValueError(
            f"model profile requires api_key: {request.model_profile_id}"
        )

    return {
        "engine": request.engine or profile.engine,
        "ip": request.ip if request.ip is not None else profile.ip,
        "port": request.port if request.port is not None else profile.port,
        "model_name": (
            request.model_name
            if request.model_name is not None
            else profile.model_name
        ),
        "page_concurrency": (
            request.page_concurrency
            if request.page_concurrency is not None
            else profile.page_concurrency
        ),
        "extra_args": {
            key: value for key, value in extra_args.items() if key != "api_key"
        },
    }

__all__ = [
    "effective_job_model_config",
    "get_model_profile_or_raise",
    "list_model_profiles",
    "resolve_model_profile_api_key",
]
