from __future__ import annotations

from typing import Callable, Generator, Optional

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ...schemas import (
    JobResponse,
    ServerEligibilityItem,
    ServerEligibilityResponse,
    ServerHeartbeatRequest,
    ServerRegisterRequest,
    ServerResponse,
)
from ..jobs.schemas import job_to_response
from . import commands, queries
from .schemas import server_to_response


GetDb = Callable[[], Generator[Session, None, None]]


def create_router(get_db: GetDb) -> APIRouter:
    router = APIRouter()

    @router.post("/api/servers/register", response_model=ServerResponse)
    def api_register_server(request: ServerRegisterRequest, session: Session = Depends(get_db)):
        return server_to_response(commands.register_server(session, request), session)

    @router.post("/api/servers/{server_id}/heartbeat", response_model=ServerResponse)
    def api_server_heartbeat(
        server_id: str,
        request: ServerHeartbeatRequest,
        session: Session = Depends(get_db),
    ):
        return server_to_response(commands.heartbeat_server(session, server_id, request), session)

    @router.get("/api/servers", response_model=list[ServerResponse])
    def api_list_servers(
        include_archived: bool = False,
        session: Session = Depends(get_db),
    ):
        return [
            server_to_response(server, session)
            for server in queries.list_servers(session, include_archived=include_archived)
        ]

    @router.delete("/api/servers/{server_id}")
    def api_archive_server(server_id: str, session: Session = Depends(get_db)):
        try:
            commands.archive_server(session, server_id)
            return {"ok": True, "archived": True}
        except commands.UnknownServerError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except commands.ServerArchiveError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @router.get("/api/servers/eligibility", response_model=ServerEligibilityResponse)
    def api_server_eligibility(input_dir: str, session: Session = Depends(get_db)):
        items = [
            ServerEligibilityItem(**item)
            for item in queries.list_server_eligibility(session, input_dir)
        ]
        return ServerEligibilityResponse(
            input_dir=input_dir,
            total_servers=len(items),
            eligible_servers=sum(1 for item in items if item.can_access),
            servers=items,
        )

    @router.post("/api/agents/{server_id}/next-job", response_model=Optional[JobResponse])
    def api_next_job(server_id: str, session: Session = Depends(get_db)):
        job = commands.claim_next_job(session, server_id)
        return job_to_response(job, session, include_secrets=True) if job is not None else None

    return router
