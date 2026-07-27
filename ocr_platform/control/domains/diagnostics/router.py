from __future__ import annotations

from typing import Callable, Generator

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from ... import database
from ...limits import ControlLimits, legacy_control_limits
from ...readiness import database_status_unavailable_body
from ...redaction import diagnostics_unavailable_message
from ...schemas import DatabaseStatusResponse
from ...settings import ControlSettings
from .metrics import PROMETHEUS_CONTENT_TYPE, render_control_metrics
from .queries import (
    agpl_license_text,
    source_offer,
    system_diagnostics,
    system_operational_diagnostics,
)


GetDb = Callable[[], Generator[Session, None, None]]


def create_router(
    get_db: GetDb,
    *,
    settings: ControlSettings | None = None,
    limits: ControlLimits | None = None,
) -> APIRouter:
    control_settings = (
        settings if settings is not None else ControlSettings.from_environment()
    )
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    router = APIRouter()

    @router.api_route("/source", methods=["GET", "HEAD"], include_in_schema=False)
    def corresponding_source() -> RedirectResponse:
        return RedirectResponse(url=str(source_offer()["source_url"]))

    @router.get("/source.json", include_in_schema=False)
    def corresponding_source_metadata() -> dict[str, object]:
        return source_offer()

    @router.get("/legal/agpl-3.0", include_in_schema=False)
    def agpl_license() -> PlainTextResponse:
        return PlainTextResponse(agpl_license_text(), media_type="text/plain; charset=utf-8")

    @router.get("/healthz")
    def api_healthz() -> dict[str, object]:
        return {"ok": True, "service": "ocr-platform-control"}

    @router.get("/readyz")
    def api_readyz(session: Session = Depends(get_db)):
        try:
            payload = system_diagnostics(
                session,
                settings=control_settings,
            )
        except Exception:  # pragma: no cover
            return JSONResponse(
                status_code=503,
                content={
                    "ok": False,
                    "service": "ocr-platform-control",
                    "error": diagnostics_unavailable_message(),
                },
            )
        return JSONResponse(status_code=200 if payload["ok"] else 503, content=payload)

    @router.get("/api/system/database", response_model=DatabaseStatusResponse)
    def api_database_status(session: Session = Depends(get_db)):
        try:
            return database.describe_database_status(session.get_bind())
        except Exception:
            return JSONResponse(
                status_code=503,
                content=database_status_unavailable_body(),
            )

    @router.get("/api/system/diagnostics")
    def api_system_diagnostics(session: Session = Depends(get_db)):
        try:
            return system_operational_diagnostics(
                session,
                strict_production=True,
                settings=control_settings,
                limits=control_limits,
            )
        except Exception:
            return JSONResponse(
                status_code=503,
                content=database_status_unavailable_body(),
            )

    @router.get(
        "/api/system/metrics",
        response_class=PlainTextResponse,
    )
    def api_system_metrics(session: Session = Depends(get_db)):
        try:
            payload = render_control_metrics(session)
        except Exception:
            return JSONResponse(
                status_code=503,
                content=database_status_unavailable_body(),
            )
        return Response(
            content=payload,
            headers={"Content-Type": PROMETHEUS_CONTENT_TYPE},
        )

    return router
