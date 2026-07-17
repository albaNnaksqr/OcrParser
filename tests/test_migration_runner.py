from __future__ import annotations

import json
from pathlib import Path

import pytest

from ocr_platform.control.database import create_session_factory
from ocr_platform.control.migrate_cli import main as migrate_main
from ocr_platform.control.migration import (
    MigrationCatalog,
    MigrationChecksumError,
    MigrationRunner,
)


def _write_test_migrations(root: Path) -> None:
    root.mkdir()
    (root / "0001_baseline.sql").write_text(
        """
        CREATE TABLE schema_migrations (
            version VARCHAR(128) PRIMARY KEY,
            applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            checksum VARCHAR(64)
        );
        CREATE TABLE example_items (id INTEGER PRIMARY KEY, name TEXT NOT NULL);
        INSERT INTO schema_migrations (version) VALUES ('0001_baseline');
        """,
        encoding="utf-8",
    )
    (root / "0002_add_item.sql").write_text(
        """
        INSERT INTO example_items (id, name) VALUES (1, 'first');
        INSERT INTO schema_migrations (version) VALUES ('0002_add_item');
        """,
        encoding="utf-8",
    )


def test_catalog_records_sha256_for_ordered_sql_files(tmp_path):
    migrations = tmp_path / "migrations"
    _write_test_migrations(migrations)

    catalog = MigrationCatalog.from_directory(migrations)

    assert catalog.versions == ["0001_baseline", "0002_add_item"]
    assert all(len(migration.checksum) == 64 for migration in catalog.migrations)


def test_postgres_runner_uses_transaction_advisory_lock():
    calls = []

    class ConnectionStub:
        dialect = type("Dialect", (), {"name": "postgresql"})()

        def execute(self, statement, params):
            calls.append((str(statement), params))

    MigrationRunner._acquire_lock(ConnectionStub())

    assert calls[0][0] == "SELECT pg_advisory_xact_lock(:lock_key)"
    assert isinstance(calls[0][1]["lock_key"], int)


def test_runner_plan_apply_status_and_verify_share_one_catalog(tmp_path):
    migrations = tmp_path / "migrations"
    _write_test_migrations(migrations)
    _, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    runner = MigrationRunner(engine, migrations_dir=migrations)

    assert [item["state"] for item in runner.plan()] == ["pending", "pending"]
    assert [item["status"] for item in runner.apply()] == ["applied", "applied"]

    status = runner.status()
    assert status["is_current"] is True
    assert status["missing_migrations"] == []
    assert status["missing_checksums"] == []
    assert status["checksum_mismatches"] == []
    assert all(item["checksum_valid"] is True for item in status["applied_migrations"])
    assert runner.verify()["verified"] is True


def test_runner_refuses_to_continue_after_applied_sql_changes(tmp_path):
    migrations = tmp_path / "migrations"
    _write_test_migrations(migrations)
    _, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")
    MigrationRunner(engine, migrations_dir=migrations).apply()

    migration = migrations / "0002_add_item.sql"
    migration.write_text(migration.read_text(encoding="utf-8") + "\n-- drift\n", encoding="utf-8")
    runner = MigrationRunner(engine, migrations_dir=migrations)

    status = runner.status()
    assert [item["version"] for item in status["checksum_mismatches"]] == ["0002_add_item"]
    assert status["is_current"] is False
    with pytest.raises(MigrationChecksumError, match="0002_add_item"):
        runner.apply()


def test_checksum_migration_backfills_historical_records(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "0001_baseline.sql").write_text(
        """
        CREATE TABLE schema_migrations (
            version VARCHAR(128) PRIMARY KEY,
            applied_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO schema_migrations (version) VALUES ('0001_baseline');
        """,
        encoding="utf-8",
    )
    (migrations / "0002_checksums.sql").write_text(
        """
        ALTER TABLE schema_migrations ADD COLUMN checksum VARCHAR(64);
        INSERT INTO schema_migrations (version) VALUES ('0002_checksums');
        """,
        encoding="utf-8",
    )
    _, engine = create_session_factory(f"sqlite:///{tmp_path / 'control.db'}")

    runner = MigrationRunner(engine, migrations_dir=migrations)
    runner.apply()

    status = runner.status()
    assert status["missing_checksums"] == []
    assert [row["checksum_valid"] for row in status["applied_migrations"]] == [True, True]


def test_migration_cli_supports_apply_status_plan_and_verify(tmp_path, capsys):
    migrations = tmp_path / "migrations"
    _write_test_migrations(migrations)
    database_url = f"sqlite:///{tmp_path / 'control.db'}"
    common = ["--database-url", database_url, "--migrations-dir", str(migrations)]

    assert migrate_main(["plan", *common]) == 0
    assert json.loads(capsys.readouterr().out)["plan"][0]["state"] == "pending"
    assert migrate_main(["apply", *common]) == 0
    assert json.loads(capsys.readouterr().out)["status"]["is_current"] is True
    assert migrate_main(["status", *common]) == 0
    assert json.loads(capsys.readouterr().out)["latest_applied_migration"] == "0002_add_item"
    assert migrate_main(["verify", *common]) == 0
    assert json.loads(capsys.readouterr().out)["verified"] is True
