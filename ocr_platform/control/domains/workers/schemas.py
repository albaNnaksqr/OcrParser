from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import Server
from ...schemas import ServerResponse
from ..common import json_loads_object
from .core import (
    count_active_jobs_for_server,
    count_running_shards_for_server,
    effective_server_status,
    is_server_stale,
)


def server_to_response(server: Server, session: Session) -> ServerResponse:
    return ServerResponse(
        id=server.id,
        name=server.name,
        host=server.host,
        status=effective_server_status(server),
        capacity_slots=server.capacity_slots,
        capabilities=json_loads_object(server.capabilities_json),
        last_heartbeat_at=server.last_heartbeat_at,
        is_stale=is_server_stale(server),
        active_jobs=count_active_jobs_for_server(session, server.id),
        running_shards=count_running_shards_for_server(session, server.id),
    )
