from __future__ import annotations

from .queries import REQUIRE_CURRENT_MIGRATIONS_ENV, env_truthy
from ... import database


def validate_current_migrations(db_engine) -> None:
    if not env_truthy(REQUIRE_CURRENT_MIGRATIONS_ENV):
        return
    status = database.describe_database_status(db_engine)
    if status.get("dialect") != "postgresql" or status.get("is_current"):
        return
    missing = ", ".join(str(item) for item in status.get("missing_migrations") or [])
    mismatches = ", ".join(
        str(item.get("version"))
        for item in status.get("checksum_mismatches") or []
        if isinstance(item, dict)
    )
    if not status.get("schema_migrations_table_exists"):
        detail = "schema_migrations table is missing"
    elif missing:
        detail = f"missing migrations: {missing}"
    elif mismatches:
        detail = f"migration checksum mismatches: {mismatches}"
    else:
        detail = f"latest applied migration: {status.get('latest_applied_migration') or 'none'}"
    raise RuntimeError(
        "PostgreSQL database migrations are not current when "
        f"{REQUIRE_CURRENT_MIGRATIONS_ENV}=1; {detail}."
    )
