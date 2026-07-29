"""Worker identity and stable assignment semantics."""

from __future__ import annotations

from datetime import datetime, timedelta

from ...models import Job, Server
from ..common import (
    POOL_SERVER_ID,
    SERVER_STALE_AFTER_SECONDS,
    json_loads_list,
    utcnow,
)


def allowed_server_ids_for_job(job: Job) -> list[str]:
    return json_loads_list(job.allowed_server_ids_json)


def server_is_allowed_for_job(job: Job, server_id: str) -> bool:
    allowed_server_ids = allowed_server_ids_for_job(job)
    return not allowed_server_ids or server_id in allowed_server_ids


def public_assigned_server_id(job: Job) -> str | None:
    return (
        None
        if job.assigned_server_id == POOL_SERVER_ID
        else job.assigned_server_id
    )


def is_server_stale(
    server: Server,
    now: datetime | None = None,
) -> bool:
    if server.last_heartbeat_at is None:
        return False
    return (now or utcnow()) - server.last_heartbeat_at > timedelta(
        seconds=SERVER_STALE_AFTER_SECONDS
    )


def effective_server_status(
    server: Server,
    now: datetime | None = None,
) -> str:
    if is_server_stale(server, now):
        return "offline"
    return server.status


__all__ = [
    "allowed_server_ids_for_job",
    "effective_server_status",
    "is_server_stale",
    "public_assigned_server_id",
    "server_is_allowed_for_job",
]
