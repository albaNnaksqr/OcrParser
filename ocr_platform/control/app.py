from __future__ import annotations

from collections.abc import AsyncIterator, Generator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

from fastapi import FastAPI
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session, sessionmaker

from . import database
from .bootstrap import (
    ControlRuntime,
    bootstrap_control_database,
    build_control_runtime,
)
from .domains.diagnostics.commands import validate_current_migrations
from .domains.diagnostics.router import create_router as create_diagnostics_router
from .domains.jobs.router import create_router as create_jobs_router
from .domains.manifests.router import create_router as create_manifests_router
from .domains.model_profiles.router import create_router as create_model_profiles_router
from .domains.remote_admin.router import create_router as create_remote_admin_router
from .domains.remote_admin.ports import RemoteWorkerPort
from .domains.workers.router import create_router as create_workers_router
from .lazy_app import _LazyControlApp
from .readiness import install_control_request_guard
from .settings import ControlSettings


def _validate_api_token_config(settings: ControlSettings) -> None:
    if settings.require_api_token and not settings.api_token:
        raise RuntimeError(
            "API token is required when OCR_PLATFORM_REQUIRE_API_TOKEN=1; "
            "set OCR_PLATFORM_API_TOKEN to a high-entropy shared secret."
        )


def _create_get_db(session_factory: sessionmaker[Session]):
    def get_db() -> Generator[Session, None, None]:
        with session_factory() as session:
            yield session

    return get_db


def _initialize_control_runtime(runtime: ControlRuntime) -> None:
    database.init_db(runtime.engine, settings=runtime.settings)
    validate_current_migrations(
        runtime.engine,
        settings=runtime.settings,
    )
    bootstrap_control_database(
        runtime.session_factory,
        runtime.engine,
    )


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


def _assemble_control_app(control_runtime: ControlRuntime) -> FastAPI:
    control_settings = control_runtime.settings

    if control_runtime.owns_engine:

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            try:
                _initialize_control_runtime(control_runtime)
                yield
            finally:
                control_runtime.engine.dispose()

        app = FastAPI(title="OCR Platform Control API", lifespan=lifespan)
    else:
        _initialize_control_runtime(control_runtime)
        app = FastAPI(title="OCR Platform Control API")

    app.state.control_settings = control_settings
    app.state.control_runtime = control_runtime
    install_control_request_guard(
        app,
        settings=control_settings,
        session_factory=control_runtime.session_factory,
    )

    get_db = _create_get_db(control_runtime.session_factory)
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
            control_runtime.remote_worker_executor,
            settings=control_settings,
        )
    )
    app.include_router(create_manifests_router(get_db))
    _register_static_ui(app)
    return app


def create_app(
    session_factory: Optional[sessionmaker[Session]] = None,
    remote_worker_executor: Optional[RemoteWorkerPort] = None,
    settings: ControlSettings | None = None,
    *,
    runtime: ControlRuntime | None = None,
) -> FastAPI:
    control_runtime = runtime
    try:
        if runtime is not None and any(
            value is not None
            for value in (
                session_factory,
                remote_worker_executor,
                settings,
            )
        ):
            raise ValueError(
                "runtime cannot be combined with session_factory, "
                "remote_worker_executor, or settings"
            )
        if runtime is not None:
            control_settings = runtime.settings
        elif settings is not None:
            control_settings = settings
        else:
            control_settings = ControlSettings.from_environment()
        _validate_api_token_config(control_settings)
        if control_runtime is None:
            control_runtime = build_control_runtime(
                settings=control_settings,
                session_factory=session_factory,
                remote_worker_executor=remote_worker_executor,
            )
        return _assemble_control_app(control_runtime)
    except BaseException:
        if control_runtime is not None and control_runtime.owns_engine:
            control_runtime.engine.dispose()
        raise


# Lazy compatibility for ``uvicorn ocr_platform.control.app:app``.
app = _LazyControlApp(create_app)
