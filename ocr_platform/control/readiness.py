from __future__ import annotations

import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session, sessionmaker
from starlette.concurrency import run_in_threadpool

from . import database
from .settings import ControlSettings


NOT_READY_CODE = "control_database_not_ready"
STATUS_UNAVAILABLE_CODE = "control_database_status_unavailable"
MIGRATION_COMMANDS = [
    "ocr-platform-migrate plan",
    "ocr-platform-migrate apply",
    "ocr-platform-migrate verify",
]
READINESS_ALLOWLIST = frozenset(
    {
        "/",
        "/healthz",
        "/readyz",
        "/api/system/database",
        "/api/system/diagnostics",
        "/source",
        "/source.json",
        "/legal/agpl-3.0",
        "/ui",
    }
)


@dataclass(frozen=True, slots=True)
class DatabaseReadiness:
    ready: bool
    reason: str | None = None


class DatabaseReadinessProbe:
    def __init__(
        self,
        session_factory: sessionmaker[Session] | None = None,
        *,
        bind_provider: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        ready_ttl_seconds: float = 1.0,
    ) -> None:
        self._session_factory = session_factory
        self._bind_provider = bind_provider
        self._clock = clock
        self._ready_ttl_seconds = max(float(ready_ttl_seconds), 0.0)
        self._cached_ready_at: float | None = None
        self._refresh_lock = threading.Lock()

    def _bind(self):
        if self._bind_provider is not None:
            return self._bind_provider()
        if self._session_factory is not None:
            with self._session_factory() as session:
                return session.get_bind()
        if database.engine is None:
            raise RuntimeError("control database is not configured")
        return database.engine

    def check(self, *, force: bool = False) -> DatabaseReadiness:
        now = self._clock()
        if (
            not force
            and self._cached_ready_at is not None
            and now - self._cached_ready_at <= self._ready_ttl_seconds
        ):
            return DatabaseReadiness(ready=True)
        with self._refresh_lock:
            now = self._clock()
            if (
                not force
                and self._cached_ready_at is not None
                and now - self._cached_ready_at <= self._ready_ttl_seconds
            ):
                return DatabaseReadiness(ready=True)
            try:
                status = database.describe_database_status(self._bind())
            except Exception:
                self._cached_ready_at = None
                return DatabaseReadiness(
                    ready=False,
                    reason="database_status_unavailable",
                )
            readiness = readiness_from_database_status(status)
            self._cached_ready_at = (
                self._clock() if readiness.ready else None
            )
            return readiness


def readiness_from_database_status(
    status: dict[str, object],
) -> DatabaseReadiness:
    if status.get("dialect") != "postgresql":
        return DatabaseReadiness(ready=True)
    if status.get("unexpected_migrations"):
        return DatabaseReadiness(False, "unexpected_migrations")
    if status.get("checksum_mismatches"):
        return DatabaseReadiness(False, "checksum_mismatch")
    if not status.get("schema_migrations_table_exists"):
        return DatabaseReadiness(False, "migration_table_missing")
    if not status.get("migration_checksum_column_exists"):
        return DatabaseReadiness(False, "checksum_column_missing")
    if status.get("missing_checksums"):
        return DatabaseReadiness(False, "migration_checksums_missing")
    if status.get("missing_migrations"):
        return DatabaseReadiness(False, "migrations_pending")
    if status.get("is_current"):
        return DatabaseReadiness(ready=True)
    return DatabaseReadiness(False, "migration_state_unknown")


def database_status_unavailable_body() -> dict[str, object]:
    return {
        "detail": {
            "code": STATUS_UNAVAILABLE_CODE,
            "reason": "database_status_unavailable",
            "commands": MIGRATION_COMMANDS,
        }
    }


def _not_ready_body(
    readiness: DatabaseReadiness,
    *,
    business_api: bool,
) -> dict[str, object]:
    payload = {
        "code": NOT_READY_CODE,
        "reason": readiness.reason or "migration_state_unknown",
        "commands": MIGRATION_COMMANDS,
    }
    if business_api:
        return {"detail": payload}
    return {
        "ok": False,
        "service": "ocr-platform-control",
        **payload,
    }


def _request_api_token(request: Request) -> str | None:
    generic_header_token = request.headers.get("X-API-Key")
    if generic_header_token:
        return generic_header_token
    platform_header_token = request.headers.get("X-OCR-Platform-Token")
    if platform_header_token:
        return platform_header_token
    authorization = request.headers.get("Authorization") or ""
    if authorization.startswith("Bearer "):
        return authorization[len("Bearer ") :]
    return None


def _is_allowlisted(path: str) -> bool:
    return path in READINESS_ALLOWLIST or path.startswith("/ui/")


def _is_existing_trailing_slash_redirect(app: FastAPI, path: str) -> bool:
    if path == "/" or not path.endswith("/"):
        return False
    canonical = path.rstrip("/")

    def route_paths(routes, prefix: str = ""):
        for route in routes:
            route_path = getattr(route, "path", None)
            if route_path is not None:
                yield f"{prefix}{route_path}"
            included = getattr(route, "original_router", None)
            if included is not None:
                context = getattr(route, "include_context", None)
                included_prefix = str(getattr(context, "prefix", ""))
                yield from route_paths(
                    included.routes,
                    f"{prefix}{included_prefix}",
                )

    return canonical in set(route_paths(app.routes))


def install_control_request_guard(
    app: FastAPI,
    *,
    settings: ControlSettings,
    session_factory: sessionmaker[Session] | None,
) -> DatabaseReadinessProbe:
    probe = DatabaseReadinessProbe(session_factory)
    app.state.database_readiness_probe = probe

    @app.middleware("http")
    async def api_token_auth(request: Request, call_next):
        configured_token = settings.api_token
        if configured_token and request.url.path.startswith("/api/"):
            request_token = _request_api_token(request)
            if request_token is None or not secrets.compare_digest(
                request_token,
                configured_token,
            ):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid API token"},
                )

        path = request.url.path
        if path == "/readyz":
            readiness = await run_in_threadpool(probe.check, force=True)
        elif (
            not path.startswith("/api/")
            or _is_allowlisted(path)
            or _is_existing_trailing_slash_redirect(
                app,
                path,
            )
        ):
            return await call_next(request)
        else:
            readiness = await run_in_threadpool(probe.check)
        if not readiness.ready:
            return JSONResponse(
                status_code=503,
                content=_not_ready_body(
                    readiness,
                    business_api=path != "/readyz",
                ),
            )
        return await call_next(request)

    return probe


__all__ = [
    "DatabaseReadiness",
    "DatabaseReadinessProbe",
    "MIGRATION_COMMANDS",
    "NOT_READY_CODE",
    "READINESS_ALLOWLIST",
    "STATUS_UNAVAILABLE_CODE",
    "database_status_unavailable_body",
    "install_control_request_guard",
    "readiness_from_database_status",
]
