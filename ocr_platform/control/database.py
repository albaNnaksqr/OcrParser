from __future__ import annotations

import os
import re
from collections.abc import Generator
from pathlib import Path
from typing import Any

from sqlalchemy import BigInteger, Engine, Integer, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


DEFAULT_DATABASE_URL = "sqlite:///./ocr_platform.db"
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_DOLLAR_QUOTE_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
SQLITE_BUSY_TIMEOUT_MS = 30000
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
BYTE_TOTAL_COLUMNS = {
    "manifests": ("total_bytes",),
    "scan_units": ("total_bytes",),
}
PRODUCTION_INDEXES = {
    "jobs": {
        "ix_jobs_status_created": (
            ("status", "created_at"),
            "CREATE INDEX IF NOT EXISTS ix_jobs_status_created ON jobs (status, created_at)",
        ),
        "ix_jobs_archived_created": (
            ("archived_at", "created_at"),
            "CREATE INDEX IF NOT EXISTS ix_jobs_archived_created "
            "ON jobs (archived_at, created_at)",
        ),
        "ix_jobs_archived_status_created": (
            ("archived_at", "status", "created_at"),
            "CREATE INDEX IF NOT EXISTS ix_jobs_archived_status_created "
            "ON jobs (archived_at, status, created_at)",
        ),
    },
    "work_shards": {
        "ix_work_shards_job_status_index": (
            ("job_id", "status", "shard_index"),
            "CREATE INDEX IF NOT EXISTS ix_work_shards_job_status_index "
            "ON work_shards (job_id, status, shard_index)"
        ),
        "ix_work_shards_job_server_status": (
            ("job_id", "assigned_server_id", "status"),
            "CREATE INDEX IF NOT EXISTS ix_work_shards_job_server_status "
            "ON work_shards (job_id, assigned_server_id, status)"
        ),
        "ix_work_shards_job_failure_status": (
            ("job_id", "failure_category", "status", "shard_index"),
            "CREATE INDEX IF NOT EXISTS ix_work_shards_job_failure_status "
            "ON work_shards (job_id, failure_category, status, shard_index)",
        ),
        "ix_work_shards_job_status_started": (
            ("job_id", "status", "started_at", "shard_index"),
            "CREATE INDEX IF NOT EXISTS ix_work_shards_job_status_started "
            "ON work_shards (job_id, status, started_at, shard_index)",
        ),
        "ux_work_shards_job_index": (
            ("job_id", "shard_index"),
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_work_shards_job_index "
            "ON work_shards (job_id, shard_index)"
        ),
        "ux_work_shards_manifest_index": (
            ("manifest_id", "shard_index"),
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_work_shards_manifest_index "
            "ON work_shards (manifest_id, shard_index)"
        ),
    },
    "scan_units": {
        "ix_scan_units_job_status": (
            ("job_id", "status"),
            "CREATE INDEX IF NOT EXISTS ix_scan_units_job_status ON scan_units (job_id, status)"
        ),
        "ux_scan_units_job_path": (
            ("job_id", "path"),
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_scan_units_job_path "
            "ON scan_units (job_id, path)",
        ),
    },
    "job_events": {
        "ix_job_events_job_created": (
            ("job_id", "created_at"),
            "CREATE INDEX IF NOT EXISTS ix_job_events_job_created ON job_events (job_id, created_at)"
        ),
        "ix_job_events_job_created_id": (
            ("job_id", "created_at", "id"),
            "CREATE INDEX IF NOT EXISTS ix_job_events_job_created_id "
            "ON job_events (job_id, created_at, id)",
        ),
        "ix_job_events_job_failure_created": (
            ("job_id", "failure_category", "created_at", "id"),
            "CREATE INDEX IF NOT EXISTS ix_job_events_job_failure_created "
            "ON job_events (job_id, failure_category, created_at, id)",
        ),
    },
    "job_files": {
        "ix_job_files_job_status": (
            ("job_id", "status"),
            "CREATE INDEX IF NOT EXISTS ix_job_files_job_status ON job_files (job_id, status)"
        ),
        "ix_job_files_job_path": (
            ("job_id", "file_path"),
            "CREATE INDEX IF NOT EXISTS ix_job_files_job_path ON job_files (job_id, file_path)",
        ),
        "ix_job_files_job_updated_id": (
            ("job_id", "updated_at", "id"),
            "CREATE INDEX IF NOT EXISTS ix_job_files_job_updated_id "
            "ON job_files (job_id, updated_at, id)",
        ),
    },
    "job_logs": {
        "ix_job_logs_job_created": (
            ("job_id", "created_at", "id"),
            "CREATE INDEX IF NOT EXISTS ix_job_logs_job_created "
            "ON job_logs (job_id, created_at, id)",
        ),
    },
    "shard_attempts": {
        "ux_shard_attempts_shard_attempt": (
            ("shard_id", "attempt_number"),
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_shard_attempts_shard_attempt "
            "ON shard_attempts (shard_id, attempt_number)",
        ),
        "ix_shard_attempts_job_status": (
            ("job_id", "status"),
            "CREATE INDEX IF NOT EXISTS ix_shard_attempts_job_status "
            "ON shard_attempts (job_id, status)",
        ),
    },
    "job_counters": {},
}


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in TRUTHY_ENV_VALUES


def _is_postgresql_url(url: str) -> bool:
    return url.startswith("postgresql://") or url.startswith("postgresql+")


def validate_database_url_for_mode(url: str) -> str:
    if _env_truthy("OCR_PLATFORM_REQUIRE_POSTGRES") and not _is_postgresql_url(url):
        raise RuntimeError(
            "PostgreSQL is required when OCR_PLATFORM_REQUIRE_POSTGRES=1; "
            f"refusing database URL: {url}"
        )
    return url


def create_session_factory(database_url: str | None = None) -> tuple[sessionmaker[Session], Engine]:
    url = validate_database_url_for_mode(
        database_url or os.environ.get("OCR_PLATFORM_DATABASE_URL") or DEFAULT_DATABASE_URL
    )
    connect_args = (
        {
            "check_same_thread": False,
            "timeout": SQLITE_BUSY_TIMEOUT_MS / 1000,
        }
        if url.startswith("sqlite")
        else {}
    )
    db_engine = create_engine(url, connect_args=connect_args, future=True)
    if url.startswith("sqlite"):

        @event.listens_for(db_engine, "connect")
        def _configure_sqlite_connection(dbapi_connection, connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False), db_engine


SessionLocal: sessionmaker[Session] | None = None
engine: Engine | None = None


def configure_database(database_url: str | None = None) -> tuple[sessionmaker[Session], Engine]:
    global SessionLocal, engine

    SessionLocal, engine = create_session_factory(database_url)
    return SessionLocal, engine


def _get_configured_database() -> tuple[sessionmaker[Session], Engine]:
    if SessionLocal is None or engine is None:
        return configure_database()
    return SessionLocal, engine


def init_db(db_engine: Engine | None = None) -> None:
    if db_engine is None:
        _, db_engine = _get_configured_database()
    Base.metadata.create_all(bind=db_engine)
    _ensure_compatible_schema(db_engine)


def _bigint_upgrade_statements(
    *,
    dialect_name: str,
    table_columns: dict[str, dict[str, object]],
) -> list[str]:
    if dialect_name != "postgresql":
        return []

    statements: list[str] = []
    for table_name, column_names in BYTE_TOTAL_COLUMNS.items():
        columns = table_columns.get(table_name, {})
        for column_name in column_names:
            column_type = columns.get(column_name)
            if column_type is None or isinstance(column_type, BigInteger):
                continue
            if isinstance(column_type, Integer):
                statements.append(
                    f"ALTER TABLE {table_name} ALTER COLUMN {column_name} "
                    f"TYPE BIGINT USING {column_name}::bigint"
                )
    return statements


def _ensure_compatible_schema(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    if "jobs" not in table_names:
        return

    job_columns = {column["name"] for column in inspector.get_columns("jobs")}
    statements: list[str] = []
    if "servers" in table_names:
        server_columns = {column["name"] for column in inspector.get_columns("servers")}
        if "archived_at" not in server_columns:
            statements.append("ALTER TABLE servers ADD COLUMN archived_at DATETIME")
    if "model_profiles" in table_names:
        model_profile_columns = {column["name"] for column in inspector.get_columns("model_profiles")}
        if "api_key_env_var" not in model_profile_columns:
            statements.append("ALTER TABLE model_profiles ADD COLUMN api_key_env_var VARCHAR(255)")

    if "failure_category" not in job_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN failure_category VARCHAR(64)")
    if "error_message" not in job_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN error_message TEXT")
    if "input_mode" not in job_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN input_mode VARCHAR(64) NOT NULL DEFAULT 'directory'")
    if "model_profile_id" not in job_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN model_profile_id VARCHAR(128)")
    if "manifest_root" not in job_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN manifest_root TEXT")
    if "target_files_per_shard" not in job_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN target_files_per_shard INTEGER NOT NULL DEFAULT 1000")
    if "max_shard_attempts" not in job_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN max_shard_attempts INTEGER NOT NULL DEFAULT 3")
    if "allowed_server_ids_json" not in job_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN allowed_server_ids_json TEXT NOT NULL DEFAULT '[]'")
    if "archived_at" not in job_columns:
        statements.append("ALTER TABLE jobs ADD COLUMN archived_at DATETIME")

    if "work_shards" in table_names:
        work_shard_columns = {column["name"] for column in inspector.get_columns("work_shards")}
        if "lease_expires_at" not in work_shard_columns:
            statements.append("ALTER TABLE work_shards ADD COLUMN lease_expires_at DATETIME")
        if "api_inflight" not in work_shard_columns:
            statements.append("ALTER TABLE work_shards ADD COLUMN api_inflight INTEGER NOT NULL DEFAULT 0")
        if "api_inflight_peak" not in work_shard_columns:
            statements.append("ALTER TABLE work_shards ADD COLUMN api_inflight_peak INTEGER NOT NULL DEFAULT 0")
        if "api_waiting" not in work_shard_columns:
            statements.append("ALTER TABLE work_shards ADD COLUMN api_waiting INTEGER NOT NULL DEFAULT 0")
        if "oldest_api_inflight" not in work_shard_columns:
            statements.append("ALTER TABLE work_shards ADD COLUMN oldest_api_inflight FLOAT NOT NULL DEFAULT 0")
        if "execution_paused" not in work_shard_columns:
            statements.append("ALTER TABLE work_shards ADD COLUMN execution_paused BOOLEAN NOT NULL DEFAULT 0")
        if "api_concurrency_limit" not in work_shard_columns:
            statements.append("ALTER TABLE work_shards ADD COLUMN api_concurrency_limit INTEGER")
        if "execution_control_reason" not in work_shard_columns:
            statements.append("ALTER TABLE work_shards ADD COLUMN execution_control_reason TEXT")

    if "manifests" in table_names:
        manifest_columns = {column["name"] for column in inspector.get_columns("manifests")}
        if "next_shard_index" not in manifest_columns:
            statements.append("ALTER TABLE manifests ADD COLUMN next_shard_index INTEGER NOT NULL DEFAULT 1")
        if "frozen_at" not in manifest_columns:
            statements.append("ALTER TABLE manifests ADD COLUMN frozen_at DATETIME")
        if "freeze_report_json" not in manifest_columns:
            statements.append("ALTER TABLE manifests ADD COLUMN freeze_report_json TEXT NOT NULL DEFAULT '{}'")
        if "worker_integrity_status" not in manifest_columns:
            statements.append("ALTER TABLE manifests ADD COLUMN worker_integrity_status VARCHAR(32)")
        if "worker_integrity_requested_at" not in manifest_columns:
            statements.append("ALTER TABLE manifests ADD COLUMN worker_integrity_requested_at DATETIME")
        if "worker_integrity_started_at" not in manifest_columns:
            statements.append("ALTER TABLE manifests ADD COLUMN worker_integrity_started_at DATETIME")
        if "worker_integrity_finished_at" not in manifest_columns:
            statements.append("ALTER TABLE manifests ADD COLUMN worker_integrity_finished_at DATETIME")
        if "worker_integrity_server_id" not in manifest_columns:
            statements.append("ALTER TABLE manifests ADD COLUMN worker_integrity_server_id VARCHAR(128)")
        if "worker_integrity_report_json" not in manifest_columns:
            statements.append("ALTER TABLE manifests ADD COLUMN worker_integrity_report_json TEXT NOT NULL DEFAULT '{}'")

    if "scan_units" in table_names:
        scan_unit_columns = {column["name"] for column in inspector.get_columns("scan_units")}
        if "failure_category" not in scan_unit_columns:
            statements.append("ALTER TABLE scan_units ADD COLUMN failure_category VARCHAR(64)")

    if "job_files" in table_names:
        job_file_columns = {column["name"] for column in inspector.get_columns("job_files")}
        if "failure_category" not in job_file_columns:
            statements.append("ALTER TABLE job_files ADD COLUMN failure_category VARCHAR(64)")

    if "job_events" in table_names:
        job_event_columns = {column["name"] for column in inspector.get_columns("job_events")}
        if "failure_category" not in job_event_columns:
            statements.append("ALTER TABLE job_events ADD COLUMN failure_category VARCHAR(64)")

    if "job_counters" in table_names:
        job_counter_columns = {column["name"] for column in inspector.get_columns("job_counters")}
        if "recent_failed_files_json" not in job_counter_columns:
            statements.append("ALTER TABLE job_counters ADD COLUMN recent_failed_files_json TEXT NOT NULL DEFAULT '[]'")
        if "recent_errors_json" not in job_counter_columns:
            statements.append("ALTER TABLE job_counters ADD COLUMN recent_errors_json TEXT NOT NULL DEFAULT '[]'")
        if "failure_category_counts_json" not in job_counter_columns:
            statements.append("ALTER TABLE job_counters ADD COLUMN failure_category_counts_json TEXT NOT NULL DEFAULT '{}'")

    if "shard_attempts" in table_names:
        shard_attempt_columns = {column["name"] for column in inspector.get_columns("shard_attempts")}
        if "execution_paused" not in shard_attempt_columns:
            statements.append("ALTER TABLE shard_attempts ADD COLUMN execution_paused BOOLEAN NOT NULL DEFAULT 0")
        if "api_concurrency_limit" not in shard_attempt_columns:
            statements.append("ALTER TABLE shard_attempts ADD COLUMN api_concurrency_limit INTEGER")
        if "execution_control_reason" not in shard_attempt_columns:
            statements.append("ALTER TABLE shard_attempts ADD COLUMN execution_control_reason TEXT")

    table_columns = {
        table_name: {
            column["name"]: column["type"]
            for column in inspector.get_columns(table_name)
        }
        for table_name in BYTE_TOTAL_COLUMNS
        if table_name in table_names
    }
    statements.extend(
        _bigint_upgrade_statements(
            dialect_name=db_engine.dialect.name,
            table_columns=table_columns,
        )
    )

    if not statements:
        _ensure_production_indexes(db_engine)
        return
    with db_engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
    _ensure_production_indexes(db_engine)


def _ensure_production_indexes(db_engine: Engine) -> None:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    statements: list[str] = []
    for table_name, index_statements in PRODUCTION_INDEXES.items():
        if table_name not in table_names:
            continue
        table_columns = {column["name"] for column in inspector.get_columns(table_name)}
        existing_indexes = {item["name"] for item in inspector.get_indexes(table_name)}
        for index_name, (required_columns, statement) in index_statements.items():
            if index_name not in existing_indexes:
                if not set(required_columns).issubset(table_columns):
                    continue
                statements.append(statement)
    if not statements:
        return
    with db_engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))


def list_known_schema_migrations(migrations_dir: Path | None = None) -> list[str]:
    migration_root = migrations_dir or MIGRATIONS_DIR
    if not migration_root.exists():
        return []
    return sorted(path.stem for path in migration_root.glob("*.sql") if path.is_file())


def _migration_sql_files(migrations_dir: Path | None = None) -> list[Path]:
    migration_root = migrations_dir or MIGRATIONS_DIR
    if not migration_root.exists():
        return []
    return sorted(path for path in migration_root.glob("*.sql") if path.is_file())


def _split_sql_script(sql: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    dollar_quote: str | None = None
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        current.append(line)
        search_from = 0
        while True:
            if dollar_quote:
                close_index = line.find(dollar_quote, search_from)
                if close_index < 0:
                    break
                search_from = close_index + len(dollar_quote)
                dollar_quote = None
                continue
            match = _DOLLAR_QUOTE_RE.search(line, search_from)
            if match is None:
                break
            dollar_quote = match.group(0)
            search_from = match.end()
        if dollar_quote is None and stripped.endswith(";"):
            statement = "\n".join(current).strip()
            if statement:
                statements.append(statement[:-1].strip())
            current = []
    trailing = "\n".join(current).strip()
    if trailing:
        statements.append(trailing)
    return statements


def _schema_migrations_exist(connection) -> bool:
    return "schema_migrations" in inspect(connection).get_table_names()


def _applied_schema_versions(connection) -> set[str]:
    if not _schema_migrations_exist(connection):
        return set()
    rows = connection.execute(text("SELECT version FROM schema_migrations")).scalars().all()
    return {str(row) for row in rows}


def apply_schema_migrations(
    db_engine: Engine,
    *,
    migrations_dir: Path | None = None,
) -> list[dict[str, str]]:
    results: list[dict[str, str]] = []
    migration_files = _migration_sql_files(migrations_dir)
    with db_engine.begin() as connection:
        applied_versions = _applied_schema_versions(connection)
        for migration_file in migration_files:
            version = migration_file.stem
            if version in applied_versions:
                results.append({"version": version, "status": "skipped"})
                continue
            sql = migration_file.read_text(encoding="utf-8")
            for statement in _split_sql_script(sql):
                connection.execute(text(statement))
            if _schema_migrations_exist(connection):
                refreshed_versions = _applied_schema_versions(connection)
                if version not in refreshed_versions:
                    connection.execute(
                        text(
                            "INSERT INTO schema_migrations (version) "
                            "VALUES (:version)"
                        ),
                        {"version": version},
                    )
                    refreshed_versions.add(version)
                applied_versions = refreshed_versions
            results.append({"version": version, "status": "applied"})
    return results


def _serialize_applied_at(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def describe_database_status(db_engine: Engine) -> dict[str, object]:
    inspector = inspect(db_engine)
    table_names = set(inspector.get_table_names())
    migrations_table_exists = "schema_migrations" in table_names
    applied_migrations: list[dict[str, str | None]] = []
    if migrations_table_exists:
        with db_engine.connect() as connection:
            rows = connection.execute(
                text(
                    "SELECT version, applied_at "
                    "FROM schema_migrations "
                    "ORDER BY applied_at ASC, version ASC"
                )
            ).mappings().all()
        applied_migrations = [
            {
                "version": str(row["version"]),
                "applied_at": _serialize_applied_at(row["applied_at"]),
            }
            for row in rows
        ]

    latest_applied = applied_migrations[-1]["version"] if applied_migrations else None
    known_migrations = list_known_schema_migrations()
    applied_versions = {migration["version"] for migration in applied_migrations}
    missing_migrations = [
        migration for migration in known_migrations if migration not in applied_versions
    ]
    return {
        "dialect": db_engine.dialect.name,
        "schema_migrations_table_exists": migrations_table_exists,
        "known_migrations": known_migrations,
        "applied_migrations": applied_migrations,
        "latest_applied_migration": latest_applied,
        "missing_migrations": missing_migrations,
        "is_current": migrations_table_exists and not missing_migrations,
    }


def get_session() -> Generator[Session, None, None]:
    session_factory, _ = _get_configured_database()
    with session_factory() as session:
        yield session
