from __future__ import annotations

import threading
from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from . import database
from .bootstrap import bootstrap_control_database
from .domains.diagnostics.commands import validate_current_migrations
from .domains.diagnostics.router import create_router as create_diagnostics_router
from .domains.jobs.router import create_router as create_jobs_router
from .domains.manifests.router import create_router as create_manifests_router
from .domains.model_profiles.router import create_router as create_model_profiles_router
from .domains.remote_admin.router import create_router as create_remote_admin_router
from .domains.workers.router import create_router as create_workers_router
from .readiness import install_control_request_guard
from .remote_workers import RemoteWorkerExecutor
from .settings import ControlSettings


def _validate_api_token_config(settings: ControlSettings) -> None:
    if settings.require_api_token and not settings.api_token:
        raise RuntimeError(
            "API token is required when OCR_PLATFORM_REQUIRE_API_TOKEN=1; "
            "set OCR_PLATFORM_API_TOKEN to a high-entropy shared secret."
        )


def _create_get_db(
    session_factory: Optional[sessionmaker[Session]],
    settings: ControlSettings,
):
    if session_factory is None:

        def get_db() -> Generator[Session, None, None]:
            yield from database.get_session(settings=settings)

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
    settings: ControlSettings | None = None,
) -> FastAPI:
    control_settings = (
        settings if settings is not None else ControlSettings.from_environment()
    )
    _validate_api_token_config(control_settings)
    remote_worker_executor = remote_worker_executor or RemoteWorkerExecutor()

    if session_factory is None:

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            database.init_db(settings=control_settings)
            if database.engine is not None:
                validate_current_migrations(
                    database.engine,
                    settings=control_settings,
                )
                if database.SessionLocal is not None:
                    bootstrap_control_database(
                        database.SessionLocal,
                        database.engine,
                    )
            yield

        app = FastAPI(title="OCR Platform Control API", lifespan=lifespan)
    else:
        with session_factory() as session:
            db_engine = session.get_bind()
            database.init_db(db_engine, settings=control_settings)
            validate_current_migrations(
                db_engine,
                settings=control_settings,
            )
        bootstrap_control_database(session_factory, db_engine)
        app = FastAPI(title="OCR Platform Control API")

    app.state.control_settings = control_settings
    install_control_request_guard(
        app,
        settings=control_settings,
        session_factory=session_factory,
    )

    get_db = _create_get_db(session_factory, control_settings)
    app.include_router(
        create_diagnostics_router(
            get_db,
            settings=control_settings,
        )
    )
    app.include_router(create_workers_router(get_db))
    app.include_router(
        create_model_profiles_router(
            get_db,
            settings=control_settings,
        )
    )
    app.include_router(
        create_jobs_router(
            get_db,
            settings=control_settings,
        )
    )
    app.include_router(
        create_remote_admin_router(
            remote_worker_executor,
            settings=control_settings,
        )
    )
    app.include_router(create_manifests_router(get_db))
    _register_static_ui(app)
    return app


class _LazyControlApp:
    """Resolve the compatibility ASGI application on first actual use."""

    def __init__(
        self,
        factory: Callable[[], FastAPI] | None = None,
    ) -> None:
        self._factory = factory or create_app
        self._application: FastAPI | None = None
        self._lock = threading.Lock()

    def _resolve(self) -> FastAPI:
        application = self._application
        if application is not None:
            return application
        with self._lock:
            application = self._application
            if application is None:
                application = self._factory()
                self._application = application
        return application

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[..., Any],
        send: Callable[..., Any],
    ) -> None:
        await self._resolve()(scope, receive, send)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._resolve(), name)

    def __repr__(self) -> str:
        state = "resolved" if self._application is not None else "unresolved"
        return f"<ocr-platform-control ASGI app ({state})>"


# Lazy compatibility for ``uvicorn ocr_platform.control.app:app``.
app = _LazyControlApp()
