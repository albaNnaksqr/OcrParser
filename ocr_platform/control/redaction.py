from __future__ import annotations

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


REDACTED_DATABASE_URL = "<invalid database URL>"
DIAGNOSTICS_UNAVAILABLE_MESSAGE = "Control diagnostics are unavailable."


def redact_database_url(value: str) -> str:
    """Return a display-safe database URL without exposing credentials."""

    try:
        return make_url(value).render_as_string(hide_password=True)
    except (ArgumentError, TypeError, ValueError):
        return REDACTED_DATABASE_URL


def diagnostics_unavailable_message() -> str:
    """Return a stable public error that never reflects exception text."""

    return DIAGNOSTICS_UNAVAILABLE_MESSAGE


__all__ = [
    "DIAGNOSTICS_UNAVAILABLE_MESSAGE",
    "REDACTED_DATABASE_URL",
    "diagnostics_unavailable_message",
    "redact_database_url",
]
