"""Worker registration, heartbeat, and pool lifecycle use cases."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

import ocr_platform.engine_provenance as engine_provenance

from ... import scheduling
from ...models import Server
from ...schemas import ServerHeartbeatRequest, ServerRegisterRequest
from ..common import POOL_SERVER_ID, json_dumps, utcnow
from . import policy


def register(
    session: Session,
    request: ServerRegisterRequest,
) -> Server:
    safe_capabilities = engine_provenance.sanitize_capabilities(
        request.capabilities
    )
    server = session.execute(
        select(Server)
        .where(Server.id == request.id)
        .with_for_update()
    ).scalar_one_or_none()
    if server is None:
        server = Server(
            id=request.id,
            name=request.name,
            host=request.host,
        )
        session.add(server)
    else:
        scheduling._fence_running_work_for_restarted_server(
            session,
            request.id,
            now=utcnow(),
        )

    policy.apply_registration(
        server,
        request,
        safe_capabilities=safe_capabilities,
        now=utcnow(),
    )
    session.flush()
    session.refresh(server)
    return server


def heartbeat(
    session: Session,
    server_id: str,
    request: ServerHeartbeatRequest,
) -> Server:
    safe_capabilities = engine_provenance.sanitize_capabilities(
        request.capabilities
    )
    server = session.execute(
        select(Server)
        .where(Server.id == server_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()
    if server is None:
        server = Server(
            id=server_id,
            name=server_id,
            host=server_id,
        )
        session.add(server)

    now = utcnow()
    policy.apply_heartbeat(
        server,
        request,
        safe_capabilities=safe_capabilities,
        now=now,
    )
    if request.status == "busy" and request.current_job_id:
        scheduling.renew_running_shard_leases(
            session,
            server_id,
            job_id=request.current_job_id,
            now=now,
        )
        scheduling.renew_running_scan_unit_leases(
            session,
            server_id,
            job_id=request.current_job_id,
            now=now,
        )
    session.flush()
    session.refresh(server)
    return server


def ensure_pool_server(session: Session) -> Server:
    server = session.get(Server, POOL_SERVER_ID)
    if server is None:
        server = Server(
            id=POOL_SERVER_ID,
            name="Server Pool",
            host="pool",
            status="online",
            capacity_slots=0,
            capabilities_json=json_dumps({"pool": True}),
            archived_at=None,
        )
        session.add(server)
        session.flush()
    elif server.archived_at is not None:
        policy.restore_pool(server)
    return server


__all__ = ["ensure_pool_server", "heartbeat", "register"]
