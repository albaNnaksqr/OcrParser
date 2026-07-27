from __future__ import annotations

import ipaddress
from importlib import import_module

from ocr_platform.optional import PLATFORM_MODULES, require_extra

from .settings import ControlSettings


_TOKEN_NOT_PROVIDED = object()


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_control_bind(
    host: str,
    *,
    api_token: str | None | object = _TOKEN_NOT_PROVIDED,
) -> None:
    if _is_loopback_host(host):
        return
    configured_token = (
        ControlSettings.from_environment().api_token
        if api_token is _TOKEN_NOT_PROVIDED
        else api_token
    )
    if not configured_token:
        raise RuntimeError(
            "OCR_PLATFORM_API_TOKEN is required when OCR_PLATFORM_HOST is not a loopback address"
        )


def main(settings: ControlSettings | None = None) -> None:
    require_extra("platform", PLATFORM_MODULES)
    uvicorn = import_module("uvicorn")
    from .app import create_app

    control_settings = (
        settings if settings is not None else ControlSettings.from_environment()
    )
    validate_control_bind(
        control_settings.host,
        api_token=control_settings.api_token,
    )
    uvicorn.run(
        create_app(settings=control_settings),
        host=control_settings.host,
        port=control_settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
