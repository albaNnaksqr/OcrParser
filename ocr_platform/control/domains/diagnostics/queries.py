from __future__ import annotations

import os

from sqlalchemy.orm import Session

from ocr_platform.legal import agpl_license_text, source_offer

from ... import database
from ..common import json_loads_object
from ..workers.core import (
    effective_server_status,
    is_server_stale,
    list_servers,
)


API_TOKEN_ENV = "OCR_PLATFORM_API_TOKEN"
REQUIRE_API_TOKEN_ENV = "OCR_PLATFORM_REQUIRE_API_TOKEN"
REQUIRE_CURRENT_MIGRATIONS_ENV = "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS"
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}


def env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY_ENV_VALUES


def api_auth_status() -> dict[str, bool]:
    configured = bool(os.environ.get(API_TOKEN_ENV))
    return {
        "enabled": configured,
        "required": env_truthy(REQUIRE_API_TOKEN_ENV),
        "configured": configured,
    }


def _server_has_writable_shared_path(capabilities: dict[str, object]) -> bool:
    shared_paths = capabilities.get("shared_paths")
    if isinstance(shared_paths, list):
        for item in shared_paths:
            if not isinstance(item, dict):
                continue
            if item.get("exists") and item.get("readable") and item.get("writable"):
                return True
    shared_roots = capabilities.get("shared_roots")
    return isinstance(shared_roots, list) and any(str(root).strip() for root in shared_roots)


def worker_diagnostics(session: Session) -> dict[str, int]:
    visible_servers = [server for server in list_servers(session) if server.archived_at is None]
    ready = stale = with_shared_roots = resource_constrained = 0
    for server in visible_servers:
        capabilities = json_loads_object(server.capabilities_json)
        if is_server_stale(server):
            stale += 1
        if _server_has_writable_shared_path(capabilities):
            with_shared_roots += 1
        pressure = capabilities.get("resource_pressure")
        if isinstance(pressure, dict) and pressure.get("constrained"):
            resource_constrained += 1
        if (
            effective_server_status(server) in {"idle", "online", "busy"}
            and not is_server_stale(server)
            and _server_has_writable_shared_path(capabilities)
        ):
            ready += 1
    return {
        "total": len(visible_servers),
        "ready": ready,
        "stale": stale,
        "with_shared_roots": with_shared_roots,
        "resource_constrained": resource_constrained,
    }


def system_diagnostics(session: Session, *, strict_production: bool = False) -> dict[str, object]:
    database_status = database.describe_database_status(session.get_bind())
    auth = api_auth_status()
    workers = worker_diagnostics(session)
    issues: list[dict[str, object]] = []
    if database_status.get("dialect") != "postgresql":
        issues.append({"severity": "warning", "code": "database_not_postgres", "message": "Production control deployments should use PostgreSQL."})
    if not database_status.get("schema_migrations_table_exists"):
        issues.append({"severity": "warning", "code": "database_migrations_missing", "message": "schema_migrations table is missing."})
    elif database_status.get("checksum_mismatches"):
        issues.append({"severity": "error", "code": "database_migration_checksum_mismatch", "message": "Control database migration checksums do not match packaged SQL.", "details": {"checksum_mismatches": database_status.get("checksum_mismatches") or []}})
    elif database_status.get("missing_checksums"):
        issues.append({"severity": "warning", "code": "database_migration_checksums_missing", "message": "Control database has migration records without checksums.", "details": {"missing_checksums": database_status.get("missing_checksums") or []}})
    elif not database_status.get("is_current"):
        issues.append({"severity": "warning", "code": "database_migration_not_current", "message": "Control database migrations are not current.", "details": {"missing_migrations": database_status.get("missing_migrations") or []}})
    if not auth["enabled"]:
        issues.append({"severity": "warning", "code": "api_auth_disabled", "message": "Control API auth is disabled."})
    if workers["total"] == 0:
        issues.append({"severity": "warning", "code": "no_workers", "message": "No workers have registered with the control API."})
    elif workers["ready"] == 0:
        issues.append({"severity": "error", "code": "no_ready_workers", "message": "No ready workers are reporting writable shared roots."})
    if workers["resource_constrained"]:
        issues.append({"severity": "warning", "code": "resource_constrained_workers", "message": "One or more workers report resource pressure.", "details": {"count": workers["resource_constrained"]}})
    ok = not any(issue["severity"] == "error" for issue in issues)
    if (strict_production or env_truthy("OCR_PLATFORM_REQUIRE_POSTGRES")) and database_status.get("dialect") != "postgresql":
        ok = False
    if env_truthy(REQUIRE_CURRENT_MIGRATIONS_ENV) and not database_status.get("is_current"):
        ok = False
    return {"ok": ok, "service": "ocr-platform-control", "database": database_status, "api_auth": auth, "workers": workers, "issues": issues}


__all__ = ["agpl_license_text", "source_offer", "system_diagnostics"]
