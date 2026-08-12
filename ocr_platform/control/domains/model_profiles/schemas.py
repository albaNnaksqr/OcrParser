from ...models import ModelProfile
from ...schemas import ModelProfileResponse
from ..common import json_loads_object
from . import certification
from .queries import resolve_model_profile_api_key


def model_profile_to_response(
    profile: ModelProfile,
) -> ModelProfileResponse:
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
        has_api_key=bool(resolve_model_profile_api_key(profile)),
        api_key_env_var=profile.api_key_env_var,
        is_default=profile.is_default,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
        certification=certification.certification_to_response(
            profile.certification
        ),
    )

__all__ = ["model_profile_to_response"]
