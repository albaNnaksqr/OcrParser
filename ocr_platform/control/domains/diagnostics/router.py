from __future__ import annotations

from typing import Callable, Generator

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from sqlalchemy.orm import Session

from ... import database
from ...schemas import DatabaseStatusResponse
from .queries import agpl_license_text, source_offer, system_diagnostics


GetDb = Callable[[], Generator[Session, None, None]]


def create_router(get_db: GetDb) -> APIRouter:
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
            payload = system_diagnostics(session)
        except Exception as exc:  # pragma: no cover
            return JSONResponse(status_code=503, content={"ok": False, "service": "ocr-platform-control", "error": str(exc)})
        return JSONResponse(status_code=200 if payload["ok"] else 503, content=payload)

    @router.get("/api/system/database", response_model=DatabaseStatusResponse)
    def api_database_status(session: Session = Depends(get_db)):
        return database.describe_database_status(session.get_bind())

    @router.get("/api/system/diagnostics")
    def api_system_diagnostics(session: Session = Depends(get_db)):
        return system_diagnostics(session, strict_production=True)

    return router
