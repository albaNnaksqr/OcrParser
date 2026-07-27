from __future__ import annotations

from fastapi import HTTPException

from ...remote_workers import default_ssh_user, validate_ssh_token
from ...schemas import RemoteWorkerScaleRequest
from ...settings import ControlSettings


def require_enabled(
    *,
    settings: ControlSettings | None = None,
) -> None:
    control_settings = (
        settings if settings is not None else ControlSettings.from_environment()
    )
    if control_settings.enable_remote_admin:
        return
    raise HTTPException(
        status_code=403,
        detail=(
            "Remote worker administration is disabled; set "
            "OCR_PLATFORM_ENABLE_REMOTE_ADMIN=1 on the control server to enable it."
        ),
    )


def validate_target(request) -> None:
    try:
        validate_ssh_token(request.host, field_name="host")
        if request.ssh_user:
            validate_ssh_token(request.ssh_user, field_name="ssh_user")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def validate_scale_request(request: RemoteWorkerScaleRequest) -> None:
    validate_target(request)
    try:
        validate_ssh_token(request.server_id_prefix, field_name="server_id_prefix")
        if request.seed_server_id:
            validate_ssh_token(request.seed_server_id, field_name="seed_server_id")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def with_default_ssh_user(request):
    if request.ssh_user:
        return request
    return request.copy(update={"ssh_user": default_ssh_user() or None})
