from __future__ import annotations

from ... import database
from ...settings import ControlSettings


REQUIRE_CURRENT_MIGRATIONS_ENV = "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS"


def validate_current_migrations(
    db_engine,
    *,
    settings: ControlSettings | None = None,
) -> None:
    control_settings = (
        settings if settings is not None else ControlSettings.from_environment()
    )
    if not control_settings.require_current_migrations:
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
