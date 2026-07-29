"""Manifest path policy and shared-path capability evaluation."""

from __future__ import annotations

import posixpath
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...models import Job, Server
from ...schemas import JobCreateRequest
from ..common import (
    DEFAULT_MANIFEST_ROOT_SUFFIX,
    POOL_SERVER_ID,
    SERVER_STALE_AFTER_SECONDS,
    json_loads_object,
    utcnow,
)


def normal_posix_path(path: str) -> str:
    normalized = posixpath.normpath(path)
    return normalized if normalized.startswith("/") else f"/{normalized}"


def path_is_under(root: str, candidate: str) -> bool:
    normalized_root = normal_posix_path(root).rstrip("/")
    normalized_candidate = normal_posix_path(candidate)
    if not normalized_root:
        normalized_root = "/"
    return normalized_candidate == normalized_root or normalized_candidate.startswith(
        normalized_root + "/"
    )


def _server_is_stale(server: Server) -> bool:
    if server.last_heartbeat_at is None:
        return False
    return utcnow() - server.last_heartbeat_at > timedelta(
        seconds=SERVER_STALE_AFTER_SECONDS
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

    stale = _server_is_stale(server)
    status = "offline" if stale else server.status
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
        if not path_is_under(path, input_dir):
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

    reason = "shared_root_unavailable" if matched_unavailable else "no_matching_shared_root"
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


def list_servers(
    session: Session,
    *,
    include_archived: bool = False,
) -> list[Server]:
    stmt = select(Server).order_by(Server.id.asc())
    if not include_archived:
        stmt = stmt.where(Server.archived_at.is_(None))
    return list(session.execute(stmt).scalars().all())


def server_is_allowed_for_job(job: Job, server_id: str) -> bool:
    from ..common import json_loads_list

    allowed_server_ids = json_loads_list(job.allowed_server_ids_json)
    return not allowed_server_ids or server_id in allowed_server_ids


def default_manifest_root_for_shared_path(shared_root: str) -> str:
    normalized_root = normal_posix_path(shared_root).rstrip("/") or "/"
    return posixpath.join(normalized_root, DEFAULT_MANIFEST_ROOT_SUFFIX)


def infer_default_manifest_root(
    session: Session,
    *,
    input_dir: str,
    input_mode: str,
    assigned_server_id: str | None,
    allowed_server_ids: list[str],
) -> str | None:
    if input_mode == "existing_manifest":
        return None
    if input_mode == "directory":
        scoped_server_ids = [assigned_server_id] if assigned_server_id else []
    else:
        scoped_server_ids = allowed_server_ids

    if scoped_server_ids:
        servers = [
            server
            for server_id in scoped_server_ids
            if server_id and server_id != POOL_SERVER_ID
            for server in [session.get(Server, server_id)]
            if server is not None and server.archived_at is None
        ]
    else:
        servers = [
            server
            for server in list_servers(session)
            if server.id != POOL_SERVER_ID
        ]

    matched_roots = {
        item["matched_path"]
        for server in servers
        for item in [evaluate_server_path_access(server, input_dir)]
        if item["can_access"] and item["matched_path"]
    }
    if len(matched_roots) != 1:
        return None
    return default_manifest_root_for_shared_path(next(iter(matched_roots)))


def server_can_access_input_dir(
    session: Session,
    server_id: str,
    input_dir: str,
) -> bool:
    server = session.get(Server, server_id)
    if server is None or server.archived_at is not None:
        return False
    return bool(evaluate_server_path_access(server, input_dir)["can_access"])


def manifest_output_dir(job: Job, request: JobCreateRequest) -> Path:
    manifest_root = request.manifest_root or job.manifest_root
    if manifest_root:
        return Path(manifest_root) / job.id
    return Path(job.output_dir) / "_manifest" / job.id


def manifest_output_dir_for_job(job: Job) -> Path:
    if job.manifest_root:
        return Path(job.manifest_root) / job.id
    return Path(job.output_dir) / "_manifest" / job.id


__all__ = [
    "default_manifest_root_for_shared_path",
    "evaluate_server_path_access",
    "infer_default_manifest_root",
    "list_servers",
    "manifest_output_dir",
    "manifest_output_dir_for_job",
    "normal_posix_path",
    "path_is_under",
    "server_can_access_input_dir",
    "server_is_allowed_for_job",
]
