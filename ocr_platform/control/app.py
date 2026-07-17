from __future__ import annotations

import os
import secrets
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from . import database
from .domains.diagnostics.commands import validate_current_migrations
from .domains.diagnostics.router import create_router as create_diagnostics_router
from .domains.jobs.router import create_router as create_jobs_router
from .domains.manifests.router import create_router as create_manifests_router
from .domains.model_profiles.router import create_router as create_model_profiles_router
from .domains.remote_admin.router import create_router as create_remote_admin_router
from .domains.workers.router import create_router as create_workers_router
from .remote_workers import RemoteWorkerExecutor


API_TOKEN_ENV = "OCR_PLATFORM_API_TOKEN"
REQUIRE_API_TOKEN_ENV = "OCR_PLATFORM_REQUIRE_API_TOKEN"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY_ENV_VALUES


def _configured_api_token() -> str | None:
    return os.environ.get(API_TOKEN_ENV) or None


def _validate_api_token_config() -> None:
    if _env_truthy(REQUIRE_API_TOKEN_ENV) and not _configured_api_token():
        raise RuntimeError(
            "API token is required when OCR_PLATFORM_REQUIRE_API_TOKEN=1; "
            "set OCR_PLATFORM_API_TOKEN to a high-entropy shared secret."
        )


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


def _create_get_db(
    session_factory: Optional[sessionmaker[Session]],
):
    if session_factory is None:

        def get_db() -> Generator[Session, None, None]:
            yield from database.get_session()

    else:

        def get_db() -> Generator[Session, None, None]:
            with session_factory() as session:
                yield session

    return get_db


def _register_static_ui(app: FastAPI) -> None:
    ui_path = Path(__file__).resolve().parent / "ui"
    if ui_path.exists():

        @app.get("/", include_in_schema=False)
        def api_root() -> RedirectResponse:
            return RedirectResponse(url="/ui/")

        app.mount("/ui", StaticFiles(directory=str(ui_path), html=True), name="ui")
        return

    @app.get("/", include_in_schema=False)
    def api_root() -> dict[str, str]:
        return {"message": "OCR Platform Control API"}


def create_app(
    session_factory: Optional[sessionmaker[Session]] = None,
    remote_worker_executor: Optional[RemoteWorkerExecutor] = None,
) -> FastAPI:
    _validate_api_token_config()
    remote_worker_executor = remote_worker_executor or RemoteWorkerExecutor()

    if session_factory is None:

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            database.init_db()
            if database.engine is not None:
                validate_current_migrations(database.engine)
            yield

        app = FastAPI(title="OCR Platform Control API", lifespan=lifespan)
    else:
        with session_factory() as session:
            validate_current_migrations(session.get_bind())
        app = FastAPI(title="OCR Platform Control API")

    @app.middleware("http")
    async def api_token_auth(request: Request, call_next):
        configured_token = _configured_api_token()
        if configured_token and request.url.path.startswith("/api/"):
            request_token = _request_api_token(request)
            if request_token is None or not secrets.compare_digest(request_token, configured_token):
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Missing or invalid API token"},
                )
        return await call_next(request)

    get_db = _create_get_db(session_factory)
    app.include_router(create_diagnostics_router(get_db))
    app.include_router(create_workers_router(get_db))
    app.include_router(create_model_profiles_router(get_db))
    app.include_router(create_jobs_router(get_db))
    app.include_router(create_remote_admin_router(remote_worker_executor))
    app.include_router(create_manifests_router(get_db))
    _register_static_ui(app)
    return app


app = create_app()
