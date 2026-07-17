"""Actionable dependency guards for optional installation profiles."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Iterable


INSTALL_COMMANDS = {
    "platform": "pip install 'ocrparser-platform[platform]'",
    "s3": "pip install 'ocrparser-platform[s3]'",
    "layout": "pip install 'ocrparser-platform[layout]'",
    "full": "pip install 'ocrparser-platform[full]'",
    "dev": "pip install 'ocrparser-platform[dev]'",
}

PLATFORM_MODULES = ("fastapi", "psycopg", "sqlalchemy", "uvicorn")


def _module_available(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return name in sys.modules


def missing_modules(modules: Iterable[str]) -> tuple[str, ...]:
    return tuple(name for name in modules if not _module_available(name))


def extra_install_message(extra: str, missing: Iterable[str]) -> str:
    command = INSTALL_COMMANDS[extra]
    missing_text = ", ".join(missing)
    return (
        f"This command requires the '{extra}' optional dependencies. "
        f"Install them with: {command}. Missing modules: {missing_text}"
    )


def require_extra(extra: str, modules: Iterable[str]) -> None:
    """Exit cleanly with the exact installation command when an extra is absent."""

    missing = missing_modules(modules)
    if not missing:
        return
    print(extra_install_message(extra, missing), file=sys.stderr)
    raise SystemExit(2)
