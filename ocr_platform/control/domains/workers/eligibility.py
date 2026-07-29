"""Worker path-access and job eligibility rules."""

from __future__ import annotations

import posixpath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Server
from ...schemas import JobCreateRequest
from ..common import POOL_SERVER_ID, json_loads_object
from .identity import effective_server_status, is_server_stale
from .projection import list_servers


def _normal_posix_path(path: str) -> str:
    normalized = posixpath.normpath(path)
    return (
        normalized
        if normalized.startswith("/")
        else f"/{normalized}"
    )


def _path_is_under(root: str, candidate: str) -> bool:
    normalized_root = _normal_posix_path(root).rstrip("/")
    normalized_candidate = _normal_posix_path(candidate)
    if not normalized_root:
        normalized_root = "/"
    return (
        normalized_candidate == normalized_root
        or normalized_candidate.startswith(normalized_root + "/")
    )


def evaluate_server_path_access(
    server: Server,
    input_dir: str,
    *,
    require_writable: bool = False,
) -> dict[str, Any]:
    if server.archived_at is not None:
        return {
            "server_id": server.id,
            "name": server.name,
            "host": server.host,
            "status": "archived",
            "is_stale": True,
            "can_access": False,
            "matched_path": None,
            "reason": "server_archived",
        }

    status = effective_server_status(server)
    stale = is_server_stale(server)
    if status == "offline" or stale:
        return {
            "server_id": server.id,
            "name": server.name,
            "host": server.host,
            "status": status,
            "is_stale": stale,
            "can_access": False,
            "matched_path": None,
            "reason": "server_offline",
        }

    try:
        capabilities = json_loads_object(server.capabilities_json)
    except (TypeError, ValueError):
        capabilities = {}
    checks = capabilities.get("shared_paths") or []
    if not isinstance(checks, list) or not checks:
        return {
            "server_id": server.id,
            "name": server.name,
            "host": server.host,
            "status": status,
            "is_stale": stale,
            "can_access": False,
            "matched_path": None,
            "reason": "no_path_checks",
        }

    matched_unavailable = None
    for check in checks:
        if not isinstance(check, dict) or not check.get("path"):
            continue
        path = str(check["path"])
        if not _path_is_under(path, input_dir):
            continue
        has_required_access = (
            check.get("exists")
            and check.get("is_dir")
            and check.get("readable")
            and (not require_writable or check.get("writable"))
        )
        if has_required_access:
            return {
                "server_id": server.id,
                "name": server.name,
                "host": server.host,
                "status": status,
                "is_stale": stale,
                "can_access": True,
                "matched_path": path,
                "reason": "ok",
            }
        matched_unavailable = path

    reason = (
        "shared_root_unavailable"
        if matched_unavailable
        else "no_matching_shared_root"
    )
    if matched_unavailable and require_writable:
        reason = "shared_root_not_writable"
    return {
        "server_id": server.id,
        "name": server.name,
        "host": server.host,
        "status": status,
        "is_stale": stale,
        "can_access": False,
        "matched_path": matched_unavailable,
        "reason": reason,
    }


def candidate_workers_for_job(
    session: Session,
    request: JobCreateRequest,
) -> list[Server | None]:
    """Return every current worker that could execute a job request."""

    explicit_ids: list[str] = []
    if (
        request.assigned_server_id
        and request.assigned_server_id != POOL_SERVER_ID
    ):
        explicit_ids.append(request.assigned_server_id)
    explicit_ids.extend(request.allowed_server_ids or [])
    explicit_ids = list(dict.fromkeys(explicit_ids))
    if explicit_ids:
        candidates: list[Server | None] = []
        for server_id in explicit_ids:
            server = (
                session.get(Server, server_id)
                if server_id != POOL_SERVER_ID
                else None
            )
            candidates.append(
                server
                if server is not None
                and server.archived_at is None
                else None
            )
        return candidates

    possible = session.execute(
        select(Server)
        .where(Server.id != POOL_SERVER_ID)
        .where(Server.archived_at.is_(None))
        .order_by(Server.id.asc())
    ).scalars().all()
    return [
        server
        for server in possible
        if evaluate_server_path_access(
            server,
            request.input_dir,
        ).get("can_access")
    ]


def list_server_eligibility(
    session: Session,
    input_dir: str,
) -> list[dict[str, Any]]:
    return [
        evaluate_server_path_access(server, input_dir)
        for server in list_servers(session)
    ]


def server_can_access_input_dir(
    session: Session,
    server_id: str,
    input_dir: str,
) -> bool:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return False
    return bool(
        evaluate_server_path_access(server, input_dir)["can_access"]
    )


__all__ = [
    "candidate_workers_for_job",
    "evaluate_server_path_access",
    "list_server_eligibility",
    "server_can_access_input_dir",
]
