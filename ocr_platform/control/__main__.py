from __future__ import annotations

import os
import ipaddress

import uvicorn


API_TOKEN_ENV = "OCR_PLATFORM_API_TOKEN"


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def validate_control_bind(host: str) -> None:
    if _is_loopback_host(host):
        return
    if not os.environ.get(API_TOKEN_ENV):
        raise RuntimeError(
            "OCR_PLATFORM_API_TOKEN is required when OCR_PLATFORM_HOST is not a loopback address"
        )


def main() -> None:
    host = os.environ.get("OCR_PLATFORM_HOST", "127.0.0.1")
    port = int(os.environ.get("OCR_PLATFORM_PORT", "8080"))
    validate_control_bind(host)
    uvicorn.run("ocr_platform.control.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
