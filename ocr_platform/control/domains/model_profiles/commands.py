from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.orm import Session

from ...models import ModelProfile, ModelProfileCertification
from ...schemas import ModelProfileRequest
from ...settings import ControlSettings
from ..common import json_dumps, saved_model_profile_keys_allowed
from . import certification, policy


class ModelProfileTransactionError(RuntimeError):
    """Raised when a Model Profile command cannot own its transaction."""


ACTIVE_TRANSACTION_ERROR = (
    "upsert_model_profile requires a session without an active transaction"
)


def _apply_model_profile(
    session: Session,
    profile_id: str,
    request: ModelProfileRequest,
    *,
    settings: ControlSettings | None = None,
) -> ModelProfile:
    policy.reject_secret_like_extra_args(
        request.extra_args,
        context="model profile",
    )
    normalized_extra_args = policy.normalize_parser_extra_args(
        request.extra_args,
        context="model profile",
    )
    certification_values = (
        certification.certification_request_values(request.certification)
        if request.certification is not None
        else None
    )
    profile = session.get(ModelProfile, profile_id)
    if profile is None:
        profile = ModelProfile(id=profile_id)
        session.add(profile)

    if request.is_default:
        session.execute(
            update(ModelProfile)
            .where(ModelProfile.id != profile_id)
            .values(is_default=False)
        )

    saved_profile_keys_disabled = not saved_model_profile_keys_allowed(
        settings
    )
    requested_saved_key = request.api_key is not None and bool(request.api_key)
    existing_saved_key_would_remain = (
        bool(profile.api_key)
        and not request.clear_api_key
        and request.api_key is None
    )
    if saved_profile_keys_disabled and requested_saved_key:
        raise ValueError(
            "saved model profile api_key is disabled; set api_key_env_var "
            "on the control server environment instead"
        )
    if saved_profile_keys_disabled and existing_saved_key_would_remain:
        raise ValueError(
            "saved model profile api_key is disabled; set clear_api_key=true "
            "and use api_key_env_var instead"
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

    if certification_values is not None:
        profile_certification = profile.certification
        if profile_certification is None:
            profile_certification = ModelProfileCertification(
                profile_id=profile_id
            )
            profile.certification = profile_certification
        for name, value in certification_values.items():
            setattr(profile_certification, name, value)

    return profile


def upsert_model_profile(
    session: Session,
    profile_id: str,
    request: ModelProfileRequest,
    *,
    settings: ControlSettings | None = None,
) -> ModelProfile:
    if session.in_transaction():
        raise ModelProfileTransactionError(ACTIVE_TRANSACTION_ERROR)

    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            profile = _apply_model_profile(
                session,
                profile_id,
                request,
                settings=settings,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return profile

__all__ = [
    "ACTIVE_TRANSACTION_ERROR",
    "ModelProfileTransactionError",
    "upsert_model_profile",
]
