from pathlib import Path
import importlib
import importlib.util
import sys

import pytest
from sqlalchemy import BigInteger, Integer, delete, inspect, text
from sqlalchemy.exc import IntegrityError

import ocr_platform.control.database as database
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.models import (
    Job,
    JobEvent,
    JobFile,
    JobLog,
    Manifest,
    ScanUnit,
    Server,
    ShardAttempt,
    WorkShard,
)


def test_init_db_creates_core_tables(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="10.0.0.1")
        job = Job(
            input_dir="/shared/in",
            output_dir="/shared/out",
            engine="dotsocr",
            assigned_server_id="server-a",
        )
        session.add(server)
        session.add(job)
        session.commit()

    assert Path(db_path).exists()


def test_sqlite_uses_busy_timeout_and_wal_for_control_plane_writes(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")

    with engine.connect() as connection:
        busy_timeout_ms = connection.execute(text("PRAGMA busy_timeout")).scalar_one()
        journal_mode = connection.execute(text("PRAGMA journal_mode")).scalar_one()

    assert busy_timeout_ms >= 30000
    assert journal_mode == "wal"


def test_require_postgres_rejects_sqlite_database_url(monkeypatch, tmp_path):
    monkeypatch.setenv("OCR_PLATFORM_REQUIRE_POSTGRES", "1")

    with pytest.raises(RuntimeError, match="PostgreSQL is required"):
        create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")


def test_require_postgres_accepts_postgresql_urls(monkeypatch):
    monkeypatch.setenv("OCR_PLATFORM_REQUIRE_POSTGRES", "true")

    assert database.validate_database_url_for_mode(
        "postgresql+psycopg://ocr:secret@db/ocr_platform"
    ) == "postgresql+psycopg://ocr:secret@db/ocr_platform"


def test_apply_schema_migrations_runs_sql_files_once(tmp_path):
    migrations_dir = tmp_path / "migrations"
    migrations_dir.mkdir()
    (migrations_dir / "0001_create_schema.sql").write_text(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version VARCHAR(128) PRIMARY KEY,
            applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE example_items (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        INSERT INTO example_items (id, name) VALUES (1, 'first');
        INSERT INTO schema_migrations (version) VALUES ('0001_create_schema');
        """,
        encoding="utf-8",
    )
    (migrations_dir / "0002_add_item.sql").write_text(
        """
        INSERT INTO example_items (id, name) VALUES (2, 'second');
        INSERT INTO schema_migrations (version) VALUES ('0002_add_item');
        """,
        encoding="utf-8",
    )
    _, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")

    first = database.apply_schema_migrations(engine, migrations_dir=migrations_dir)
    second = database.apply_schema_migrations(engine, migrations_dir=migrations_dir)

    assert [item["version"] for item in first] == ["0001_create_schema", "0002_add_item"]
    assert [item["status"] for item in first] == ["applied", "applied"]
    assert [item["version"] for item in second] == ["0001_create_schema", "0002_add_item"]
    assert [item["status"] for item in second] == ["skipped", "skipped"]
    with engine.connect() as connection:
        rows = connection.execute(text("SELECT id, name FROM example_items ORDER BY id")).all()
        migrations = connection.execute(text("SELECT version FROM schema_migrations ORDER BY version")).scalars().all()
    assert rows == [(1, "first"), (2, "second")]
    assert migrations == ["0001_create_schema", "0002_add_item"]


def test_apply_control_migrations_tool_exposes_database_url_and_migrations_dir_options():
    tool_path = Path(__file__).resolve().parents[1] / "tools" / "apply_control_migrations.py"
    spec = importlib.util.spec_from_file_location("apply_control_migrations", tool_path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)

    parser = module.build_parser()
    args = parser.parse_args(
        [
            "--database-url",
            "postgresql+psycopg://ocr:secret@db/ocr_platform",
            "--migrations-dir",
            "/opt/ocr-platform/migrations",
        ]
    )

    assert args.database_url == "postgresql+psycopg://ocr:secret@db/ocr_platform"
    assert args.migrations_dir == "/opt/ocr-platform/migrations"


def test_sql_script_splitter_keeps_postgres_dollar_quoted_blocks_together():
    sql = """
    CREATE TABLE example_items (id INTEGER PRIMARY KEY);

    DO $$
    BEGIN
        RAISE EXCEPTION 'keep this semicolon inside the block';
    END
    $$;

    INSERT INTO schema_migrations (version)
    VALUES ('0002_dollar_block')
    ON CONFLICT (version) DO NOTHING;
    """

    statements = database._split_sql_script(sql)

    assert len(statements) == 3
    assert statements[0].startswith("CREATE TABLE example_items")
    assert statements[1].startswith("DO $$")
    assert "keep this semicolon inside the block" in statements[1]
    assert statements[1].endswith("$$")
    assert statements[2].startswith("INSERT INTO schema_migrations")


def test_init_db_creates_expected_tables(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    inspector = inspect(engine)

    assert set(inspector.get_table_names()) == {
        "job_events",
        "job_counters",
        "job_files",
        "job_logs",
        "jobs",
        "manifests",
        "model_profiles",
        "scan_units",
        "servers",
        "shard_attempts",
        "work_shards",
    }


def test_init_db_creates_production_query_indexes(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    inspector = inspect(engine)
    indexes = {
        table_name: {item["name"] for item in inspector.get_indexes(table_name)}
        for table_name in (
            "work_shards",
            "scan_units",
            "job_events",
            "job_files",
            "jobs",
            "shard_attempts",
            "job_logs",
        )
    }

    assert "ix_work_shards_job_status_index" in indexes["work_shards"]
    assert "ix_work_shards_job_server_status" in indexes["work_shards"]
    assert "ix_work_shards_job_failure_status" in indexes["work_shards"]
    assert "ix_work_shards_job_status_started" in indexes["work_shards"]
    assert "ux_work_shards_job_index" in indexes["work_shards"]
    assert "ux_work_shards_manifest_index" in indexes["work_shards"]
    assert "ix_scan_units_job_status" in indexes["scan_units"]
    assert "ux_scan_units_job_path" in indexes["scan_units"]
    assert "ix_job_events_job_created" in indexes["job_events"]
    assert "ix_job_events_job_created_id" in indexes["job_events"]
    assert "ix_job_events_job_failure_created" in indexes["job_events"]
    assert "ix_job_files_job_status" in indexes["job_files"]
    assert "ix_job_files_job_updated_id" in indexes["job_files"]
    assert "ix_job_files_job_path" in indexes["job_files"]
    assert "ix_jobs_status_created" in indexes["jobs"]
    assert "ix_jobs_archived_created" in indexes["jobs"]
    assert "ix_jobs_archived_status_created" in indexes["jobs"]
    assert "ux_shard_attempts_shard_attempt" in indexes["shard_attempts"]
    assert "ix_job_logs_job_created" in indexes["job_logs"]


def test_production_sql_migration_baseline_covers_core_schema_and_indexes():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0001_control_schema.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in normalized_sql
    assert "0001_control_schema" in normalized_sql
    assert "recent_failed_files_json TEXT NOT NULL DEFAULT '[]'" in normalized_sql
    assert "recent_errors_json TEXT NOT NULL DEFAULT '[]'" in normalized_sql
    assert "failure_category_counts_json TEXT NOT NULL DEFAULT '{}'" in normalized_sql
    for table_name in (
        "servers",
        "model_profiles",
        "jobs",
        "manifests",
        "work_shards",
        "shard_attempts",
        "scan_units",
        "job_events",
        "job_files",
        "job_logs",
        "job_counters",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table_name}" in normalized_sql
    for index_statements in database.PRODUCTION_INDEXES.values():
        for _, statement in index_statements.values():
            assert " ".join(statement.split()) in normalized_sql


def test_incremental_migration_enforces_global_work_shard_index():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0002_enforce_work_shard_job_index.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_work_shards_job_index ON work_shards (job_id, shard_index)" in normalized_sql
    assert "0002_enforce_work_shard_job_index" in normalized_sql


def test_incremental_migration_adds_job_counter_failed_file_samples():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0003_job_counter_failed_file_samples.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "ALTER TABLE job_counters ADD COLUMN IF NOT EXISTS recent_failed_files_json TEXT NOT NULL DEFAULT '[]'" in normalized_sql
    assert "0003_job_counter_failed_file_samples" in normalized_sql


def test_incremental_migration_adds_job_counter_failure_category_counts():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0013_job_counter_failure_category_counts.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "ALTER TABLE job_counters ADD COLUMN IF NOT EXISTS failure_category_counts_json TEXT NOT NULL DEFAULT '{}'" in normalized_sql
    assert "0013_job_counter_failure_category_counts" in normalized_sql


def test_incremental_migration_adds_job_event_failure_category_index():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0014_job_event_failure_category.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "ALTER TABLE job_events ADD COLUMN IF NOT EXISTS failure_category VARCHAR(64)" in normalized_sql
    assert "CREATE INDEX IF NOT EXISTS ix_job_events_job_failure_created ON job_events (job_id, failure_category, created_at, id)" in normalized_sql
    assert "0014_job_event_failure_category" in normalized_sql


def test_incremental_migration_adds_job_counter_recent_error_samples():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0015_job_counter_recent_error_samples.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "ALTER TABLE job_counters ADD COLUMN IF NOT EXISTS recent_errors_json TEXT NOT NULL DEFAULT '[]'" in normalized_sql
    assert "0015_job_counter_recent_error_samples" in normalized_sql


def test_incremental_migration_adds_job_file_upsert_path_index():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0016_job_file_upsert_path_index.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "CREATE INDEX IF NOT EXISTS ix_job_files_job_path ON job_files (job_id, file_path)" in normalized_sql
    assert "0016_job_file_upsert_path_index" in normalized_sql


def test_incremental_migration_enforces_unique_shard_attempt_number():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0004_unique_shard_attempt_number.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_shard_attempts_shard_attempt ON shard_attempts (shard_id, attempt_number)" in normalized_sql
    assert "0004_unique_shard_attempt_number" in normalized_sql


def test_incremental_migration_adds_job_file_failure_category():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0005_job_file_failure_category.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "ALTER TABLE job_files ADD COLUMN IF NOT EXISTS failure_category VARCHAR(64)" in normalized_sql
    assert "0005_job_file_failure_category" in normalized_sql


def test_incremental_migration_adds_job_log_pruning_index():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0006_job_log_pruning_index.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "CREATE INDEX IF NOT EXISTS ix_job_logs_job_created ON job_logs (job_id, created_at, id)" in normalized_sql
    assert "0006_job_log_pruning_index" in normalized_sql


def test_incremental_migration_adds_shard_attempt_execution_control():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0007_shard_attempt_execution_control.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "ALTER TABLE shard_attempts ADD COLUMN IF NOT EXISTS execution_paused BOOLEAN NOT NULL DEFAULT FALSE" in normalized_sql
    assert "ALTER TABLE shard_attempts ADD COLUMN IF NOT EXISTS api_concurrency_limit INTEGER" in normalized_sql
    assert "ALTER TABLE shard_attempts ADD COLUMN IF NOT EXISTS execution_control_reason TEXT" in normalized_sql
    assert "0007_shard_attempt_execution_control" in normalized_sql


def test_incremental_migration_adds_detail_pruning_indexes():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0008_detail_pruning_indexes.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "CREATE INDEX IF NOT EXISTS ix_job_events_job_created_id ON job_events (job_id, created_at, id)" in normalized_sql
    assert "CREATE INDEX IF NOT EXISTS ix_job_files_job_updated_id ON job_files (job_id, updated_at, id)" in normalized_sql
    assert "0008_detail_pruning_indexes" in normalized_sql


def test_incremental_migration_adds_compatibility_schema_columns():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0009_compatibility_schema_columns.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "ALTER TABLE servers ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ" in normalized_sql
    assert "ALTER TABLE model_profiles ADD COLUMN IF NOT EXISTS api_key_env_var VARCHAR(255)" in normalized_sql
    assert "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS failure_category VARCHAR(64)" in normalized_sql
    assert "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS allowed_server_ids_json TEXT NOT NULL DEFAULT '[]'" in normalized_sql
    assert "ALTER TABLE work_shards ADD COLUMN IF NOT EXISTS execution_paused BOOLEAN NOT NULL DEFAULT FALSE" in normalized_sql
    assert "ALTER TABLE work_shards ADD COLUMN IF NOT EXISTS api_concurrency_limit INTEGER" in normalized_sql
    assert "ALTER TABLE manifests ADD COLUMN IF NOT EXISTS next_shard_index INTEGER NOT NULL DEFAULT 1" in normalized_sql
    assert "ALTER TABLE manifests ADD COLUMN IF NOT EXISTS freeze_report_json TEXT NOT NULL DEFAULT '{}'" in normalized_sql
    assert "ALTER TABLE scan_units ADD COLUMN IF NOT EXISTS failure_category VARCHAR(64)" in normalized_sql
    assert "ALTER TABLE job_counters ADD COLUMN IF NOT EXISTS recent_errors_json TEXT NOT NULL DEFAULT '[]'" in normalized_sql
    assert "0009_compatibility_schema_columns" in normalized_sql


def test_incremental_migration_adds_shard_inspector_filter_indexes():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0010_shard_inspector_filter_indexes.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "CREATE INDEX IF NOT EXISTS ix_work_shards_job_failure_status ON work_shards (job_id, failure_category, status, shard_index)" in normalized_sql
    assert "CREATE INDEX IF NOT EXISTS ix_work_shards_job_status_started ON work_shards (job_id, status, started_at, shard_index)" in normalized_sql
    assert "0010_shard_inspector_filter_indexes" in normalized_sql


def test_incremental_migration_enforces_unique_scan_unit_path():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0011_unique_scan_unit_path.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "duplicate scan_units rows exist for the same job_id/path" in normalized_sql
    assert "GROUP BY job_id, path HAVING COUNT(*) > 1" in normalized_sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS ux_scan_units_job_path ON scan_units (job_id, path)" in normalized_sql
    assert "0011_unique_scan_unit_path" in normalized_sql


def test_incremental_migration_adds_default_jobs_list_index():
    migration_path = (
        Path(database.__file__).parent
        / "migrations"
        / "0012_jobs_default_list_index.sql"
    )

    assert migration_path.exists()
    sql = migration_path.read_text(encoding="utf-8")
    normalized_sql = " ".join(sql.split())
    assert "CREATE INDEX IF NOT EXISTS ix_jobs_archived_created ON jobs (archived_at, created_at)" in normalized_sql
    assert "0012_jobs_default_list_index" in normalized_sql


def test_init_db_adds_shard_lease_column_to_existing_sqlite_schema(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE work_shards (id INTEGER PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE scan_units (id INTEGER PRIMARY KEY)"))

    init_db(engine)

    inspector = inspect(engine)
    work_shard_columns = {column["name"] for column in inspector.get_columns("work_shards")}
    assert "lease_expires_at" in work_shard_columns
    assert "execution_paused" in work_shard_columns
    assert "api_concurrency_limit" in work_shard_columns
    assert "execution_control_reason" in work_shard_columns
    scan_unit_columns = {column["name"] for column in inspector.get_columns("scan_units")}
    assert "failure_category" in scan_unit_columns


def test_init_db_adds_execution_control_columns_to_existing_shard_attempts(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE shard_attempts (id INTEGER PRIMARY KEY)"))

    init_db(engine)

    inspector = inspect(engine)
    shard_attempt_columns = {column["name"] for column in inspector.get_columns("shard_attempts")}
    assert "execution_paused" in shard_attempt_columns
    assert "api_concurrency_limit" in shard_attempt_columns
    assert "execution_control_reason" in shard_attempt_columns


def test_init_db_adds_job_counter_failed_file_samples_to_existing_schema(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE job_counters ("
                "job_id VARCHAR(36) PRIMARY KEY, "
                "started_files INTEGER NOT NULL DEFAULT 0)"
            )
        )

    init_db(engine)

    inspector = inspect(engine)
    job_counter_columns = {column["name"] for column in inspector.get_columns("job_counters")}
    assert "recent_failed_files_json" in job_counter_columns


def test_init_db_adds_job_counter_failure_category_counts_to_existing_schema(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE job_counters ("
                "job_id VARCHAR(36) PRIMARY KEY, "
                "started_files INTEGER NOT NULL DEFAULT 0)"
            )
        )

    init_db(engine)

    inspector = inspect(engine)
    job_counter_columns = {column["name"] for column in inspector.get_columns("job_counters")}
    assert "failure_category_counts_json" in job_counter_columns


def test_init_db_adds_job_counter_recent_error_samples_to_existing_schema(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE job_counters ("
                "job_id VARCHAR(36) PRIMARY KEY, "
                "started_files INTEGER NOT NULL DEFAULT 0)"
            )
        )

    init_db(engine)

    inspector = inspect(engine)
    job_counter_columns = {column["name"] for column in inspector.get_columns("job_counters")}
    assert "recent_errors_json" in job_counter_columns


def test_init_db_adds_job_file_failure_category_to_existing_schema(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE job_files ("
                "id INTEGER PRIMARY KEY, "
                "job_id VARCHAR(36), "
                "file_path TEXT NOT NULL, "
                "filename VARCHAR(512) NOT NULL, "
                "status VARCHAR(32) NOT NULL DEFAULT 'pending')"
            )
        )

    init_db(engine)

    inspector = inspect(engine)
    job_file_columns = {column["name"] for column in inspector.get_columns("job_files")}
    assert "failure_category" in job_file_columns


def test_init_db_adds_model_profile_api_key_env_var_to_existing_schema(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE jobs (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(
            text(
                "CREATE TABLE model_profiles ("
                "id VARCHAR(128) PRIMARY KEY, "
                "label VARCHAR(255) NOT NULL, "
                "engine VARCHAR(64) NOT NULL, "
                "extra_args_json TEXT NOT NULL DEFAULT '{}', "
                "api_key TEXT, "
                "requires_api_key BOOLEAN NOT NULL DEFAULT FALSE)"
            )
        )

    init_db(engine)

    inspector = inspect(engine)
    model_profile_columns = {column["name"] for column in inspector.get_columns("model_profiles")}
    assert "api_key_env_var" in model_profile_columns


def test_manifest_byte_totals_use_bigint_for_large_batches(tmp_path):
    db_path = tmp_path / "control.db"
    _, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    inspector = inspect(engine)
    manifest_columns = {column["name"]: column for column in inspector.get_columns("manifests")}
    scan_unit_columns = {column["name"]: column for column in inspector.get_columns("scan_units")}

    assert isinstance(manifest_columns["total_bytes"]["type"], BigInteger)
    assert isinstance(scan_unit_columns["total_bytes"]["type"], BigInteger)


def test_postgresql_startup_migration_upgrades_byte_totals_to_bigint():
    statements = database._bigint_upgrade_statements(
        dialect_name="postgresql",
        table_columns={
            "manifests": {"total_bytes": Integer()},
            "scan_units": {"total_bytes": Integer()},
        },
    )

    assert statements == [
        "ALTER TABLE manifests ALTER COLUMN total_bytes TYPE BIGINT USING total_bytes::bigint",
        "ALTER TABLE scan_units ALTER COLUMN total_bytes TYPE BIGINT USING total_bytes::bigint",
    ]


def test_input_mode_migration_widens_distributed_mode_columns():
    root = Path(__file__).resolve().parents[1]
    baseline = (
        root / "ocr_platform" / "control" / "migrations" / "0001_control_schema.sql"
    ).read_text(encoding="utf-8")
    migration = (
        root
        / "ocr_platform"
        / "control"
        / "migrations"
        / "0018_widen_input_mode_columns.sql"
    ).read_text(encoding="utf-8")

    assert "input_mode VARCHAR(64) NOT NULL DEFAULT 'directory'" in baseline
    assert "input_mode VARCHAR(64) NOT NULL" in baseline
    assert "ALTER TABLE jobs" in migration
    assert "ALTER TABLE manifests" in migration
    assert "ALTER COLUMN input_mode TYPE VARCHAR(64)" in migration
    assert "0018_widen_input_mode_columns" in migration


def test_manifest_and_work_shard_models_persist(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="localhost")
        session.add(server)
        job = Job(
            input_dir="/shared/in",
            output_dir="/shared/out",
            engine="dotsocr",
            assigned_server_id="server-a",
        )
        session.add(job)
        session.flush()

        manifest = Manifest(
            job_id=job.id,
            input_mode="folder_snapshot",
            input_root="/shared/in",
            manifest_path="/shared/manifests/job/manifest.jsonl",
            meta_path="/shared/manifests/job/manifest.meta.json",
            file_count=3,
            total_bytes=100,
            status="ready",
        )
        session.add(manifest)
        session.flush()

        shard = WorkShard(
            job_id=job.id,
            manifest_id=manifest.id,
            shard_index=1,
            shard_path="/shared/manifests/job/shards/shard-000001.jsonl",
            status="pending",
            file_count=3,
        )
        session.add(shard)
        session.commit()

        assert session.query(Manifest).count() == 1
        assert session.query(WorkShard).count() == 1


def test_work_shards_reject_duplicate_job_shard_index_across_manifests(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="localhost")
        job = Job(
            input_dir="/shared/in",
            output_dir="/shared/out",
            engine="dotsocr",
            assigned_server=server,
        )
        session.add(job)
        session.flush()
        first_manifest = Manifest(
            job_id=job.id,
            input_mode="distributed_remote_folder_snapshot",
            input_root="/shared/in/a",
            manifest_path="/shared/manifests/job/scan-a.jsonl",
            file_count=1,
            total_bytes=10,
            status="ready",
        )
        second_manifest = Manifest(
            job_id=job.id,
            input_mode="distributed_remote_folder_snapshot",
            input_root="/shared/in/b",
            manifest_path="/shared/manifests/job/scan-b.jsonl",
            file_count=1,
            total_bytes=20,
            status="ready",
        )
        session.add_all([first_manifest, second_manifest])
        session.flush()
        session.add_all(
            [
                WorkShard(
                    job_id=job.id,
                    manifest_id=first_manifest.id,
                    shard_index=1,
                    shard_path="/shared/manifests/job/shards/shard-000001.jsonl",
                    status="pending",
                    file_count=1,
                ),
                WorkShard(
                    job_id=job.id,
                    manifest_id=second_manifest.id,
                    shard_index=1,
                    shard_path="/shared/manifests/job/shards/shard-000001-duplicate.jsonl",
                    status="pending",
                    file_count=1,
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_shard_attempt_model_persists_attempt_evidence(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="localhost")
        job = Job(
            input_dir="/shared/in",
            output_dir="/shared/out",
            engine="dotsocr",
            assigned_server=server,
        )
        session.add(job)
        session.flush()
        manifest = Manifest(
            job_id=job.id,
            input_mode="folder_snapshot",
            input_root="/shared/in",
            manifest_path="/shared/manifests/job/manifest.jsonl",
            file_count=1,
            total_bytes=10,
            status="ready",
        )
        session.add(manifest)
        session.flush()
        shard = WorkShard(
            job_id=job.id,
            manifest_id=manifest.id,
            shard_index=1,
            shard_path="/shared/manifests/job/shards/shard-000001.jsonl",
            status="running",
            assigned_server_id="server-a",
            attempt_count=1,
            file_count=1,
        )
        session.add(shard)
        session.flush()
        session.add(
            ShardAttempt(
                job_id=job.id,
                shard_id=shard.id,
                attempt_number=1,
                server_id="server-a",
                status="running",
            )
        )
        session.commit()

        attempt = session.query(ShardAttempt).one()
        assert attempt.job_id == job.id
        assert attempt.shard_id == shard.id
        assert attempt.attempt_number == 1
        assert attempt.server_id == "server-a"
        assert attempt.status == "running"


def test_shard_attempt_rejects_duplicate_attempt_number_for_shard(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="localhost")
        job = Job(
            input_dir="/shared/in",
            output_dir="/shared/out",
            engine="dotsocr",
            assigned_server=server,
        )
        session.add(job)
        session.flush()
        manifest = Manifest(
            job_id=job.id,
            input_mode="folder_snapshot",
            input_root="/shared/in",
            manifest_path="/shared/manifests/job/manifest.jsonl",
            file_count=1,
            total_bytes=10,
            status="ready",
        )
        session.add(manifest)
        session.flush()
        shard = WorkShard(
            job_id=job.id,
            manifest_id=manifest.id,
            shard_index=1,
            shard_path="/shared/manifests/job/shards/shard-000001.jsonl",
            status="running",
            assigned_server_id="server-a",
            attempt_count=1,
            file_count=1,
        )
        session.add(shard)
        session.flush()
        session.add_all(
            [
                ShardAttempt(
                    job_id=job.id,
                    shard_id=shard.id,
                    attempt_number=1,
                    server_id="server-a",
                    status="running",
                ),
                ShardAttempt(
                    job_id=job.id,
                    shard_id=shard.id,
                    attempt_number=1,
                    server_id="server-b",
                    status="running",
                ),
            ]
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_manifest_tracks_next_shard_index_for_concurrent_scan_completion(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="10.0.0.1")
        job = Job(
            input_dir="/shared/in",
            output_dir="/shared/out",
            engine="dotsocr",
            assigned_server=server,
        )
        session.add(job)
        session.flush()
        manifest = Manifest(
            job_id=job.id,
            input_mode="distributed_remote_folder_snapshot",
            input_root="/shared/in",
            manifest_path="/shared/manifests/job/manifest.jsonl",
            meta_path="/shared/manifests/job/manifest.meta.json",
            file_count=0,
            total_bytes=0,
            status="scanning",
        )
        session.add(manifest)
        session.add(ScanUnit(job_id=job.id, path="/shared/in/a", status="pending"))
        session.commit()
        session.refresh(manifest)

        assert manifest.next_shard_index == 1


def test_sqlite_enforces_job_server_foreign_key(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        session.add(
            Job(
                input_dir="/shared/in",
                output_dir="/shared/out",
                engine="dotsocr",
                assigned_server_id="missing",
            )
        )

        with pytest.raises(IntegrityError):
            session.commit()


def test_sqlite_timestamps_round_trip_as_aware_utc(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="10.0.0.1")
        session.add(server)
        session.commit()
        session.refresh(server)

        assert server.created_at.tzinfo is not None
        assert server.created_at.utcoffset().total_seconds() == 0


def test_server_and_job_defaults(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="10.0.0.1")
        job = Job(
            input_dir="/shared/in",
            output_dir="/shared/out",
            engine="dotsocr",
            assigned_server=server,
        )
        session.add(job)
        session.commit()
        session.refresh(server)
        session.refresh(job)

        assert server.status == "offline"
        assert server.capacity_slots == 1
        assert server.capabilities_json == "{}"
        assert job.status == "queued"
        assert job.extra_args_json == "{}"
        assert job.command_json == "[]"
        assert job.max_shard_attempts == 3
        assert job.force_reprocess is False
        assert job.stop_requested is False


def test_deleting_job_cascades_to_child_rows(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="10.0.0.1")
        job = Job(
            input_dir="/shared/in",
            output_dir="/shared/out",
            engine="dotsocr",
            assigned_server=server,
            files=[
                JobFile(file_path="/shared/in/a.pdf", filename="a.pdf"),
            ],
            events=[
                JobEvent(event_type="job.created"),
            ],
            logs=[
                JobLog(server_id="server-a", stream="stdout", line="started"),
            ],
        )
        session.add(job)
        session.flush()
        manifest = Manifest(
            job_id=job.id,
            input_mode="folder_snapshot",
            input_root="/shared/in",
            manifest_path="/shared/manifests/job/manifest.jsonl",
            meta_path="/shared/manifests/job/manifest.meta.json",
            file_count=1,
            total_bytes=100,
            status="ready",
        )
        session.add(manifest)
        session.flush()
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=1,
                shard_path="/shared/manifests/job/shards/shard-000001.jsonl",
                status="pending",
                file_count=1,
            )
        )
        session.commit()

        session.delete(job)
        session.commit()

        assert session.query(JobFile).count() == 0
        assert session.query(JobEvent).count() == 0
        assert session.query(JobLog).count() == 0
        assert session.query(Manifest).count() == 0
        assert session.query(WorkShard).count() == 0


def test_bulk_deleting_job_cascades_to_child_rows_in_database(tmp_path):
    db_path = tmp_path / "control.db"
    session_factory, engine = create_session_factory(f"sqlite:///{db_path}")
    init_db(engine)

    with session_factory() as session:
        server = Server(id="server-a", name="Server A", host="10.0.0.1")
        job = Job(
            input_dir="/shared/in",
            output_dir="/shared/out",
            engine="dotsocr",
            assigned_server=server,
            files=[
                JobFile(file_path="/shared/in/a.pdf", filename="a.pdf"),
            ],
            events=[
                JobEvent(event_type="job.created"),
            ],
            logs=[
                JobLog(server_id="server-a", stream="stdout", line="started"),
            ],
        )
        session.add(job)
        session.flush()
        manifest = Manifest(
            job_id=job.id,
            input_mode="folder_snapshot",
            input_root="/shared/in",
            manifest_path="/shared/manifests/job/manifest.jsonl",
            meta_path="/shared/manifests/job/manifest.meta.json",
            file_count=1,
            total_bytes=100,
            status="ready",
        )
        session.add(manifest)
        session.flush()
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=1,
                shard_path="/shared/manifests/job/shards/shard-000001.jsonl",
                status="pending",
                file_count=1,
            )
        )
        session.commit()
        job_id = job.id

        session.execute(delete(Job).where(Job.id == job_id))
        session.commit()

        assert session.query(JobFile).count() == 0
        assert session.query(JobEvent).count() == 0
        assert session.query(JobLog).count() == 0
        assert session.query(Manifest).count() == 0
        assert session.query(WorkShard).count() == 0


def test_database_import_does_not_configure_global_engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    reloaded_database = importlib.reload(database)

    assert reloaded_database.engine is None
    assert reloaded_database.SessionLocal is None
    assert not (tmp_path / "ocr_platform.db").exists()
