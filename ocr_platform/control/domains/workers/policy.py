"""Worker aggregate state and capability policy."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...models import Server
from ...schemas import ServerHeartbeatRequest, ServerRegisterRequest
from ..common import json_dumps, json_loads_object


def apply_registration(
    server: Server,
    request: ServerRegisterRequest,
    *,
    safe_capabilities: dict[str, Any],
    now: datetime,
) -> None:
    server.name = request.name
    server.host = request.host
    server.capacity_slots = request.capacity_slots
    server.capabilities_json = json_dumps(safe_capabilities)
    server.status = "online"
    server.last_heartbeat_at = now
    server.archived_at = None


def apply_heartbeat(
    server: Server,
    request: ServerHeartbeatRequest,
    *,
    safe_capabilities: dict[str, Any],
    now: datetime,
) -> None:
    existing_capabilities = json_loads_object(server.capabilities_json)
    merged_capabilities = {**existing_capabilities, **safe_capabilities}
    server.status = request.status
    server.capabilities_json = json_dumps(merged_capabilities)
    server.last_heartbeat_at = now
    server.archived_at = None


def restore_pool(server: Server) -> None:
    server.archived_at = None


def archive(server: Server, *, now: datetime) -> None:
    server.archived_at = now


__all__ = [
    "apply_heartbeat",
    "apply_registration",
    "archive",
    "restore_pool",
]
