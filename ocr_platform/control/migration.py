from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import Engine, inspect, text


MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_DOLLAR_QUOTE_RE = re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$")
_ADVISORY_LOCK_KEY = int.from_bytes(
    hashlib.sha256(b"ocrparser-platform-schema-migrations").digest()[:8],
    byteorder="big",
    signed=True,
)


class MigrationError(RuntimeError):
    pass


class MigrationChecksumError(MigrationError):
    pass


@dataclass(frozen=True)
class Migration:
    version: str
    path: Path
    checksum: str

    @classmethod
    def from_path(cls, path: Path) -> "Migration":
        return cls(
            version=path.stem,
            path=path,
            checksum=hashlib.sha256(path.read_bytes()).hexdigest(),
        )


@dataclass(frozen=True)
class MigrationCatalog:
    migrations: tuple[Migration, ...]

    @classmethod
    def from_directory(cls, migrations_dir: Path | None = None) -> "MigrationCatalog":
        root = Path(migrations_dir or MIGRATIONS_DIR)
        if not root.exists():
            return cls(())
        migrations = tuple(
            Migration.from_path(path)
            for path in sorted(root.glob("*.sql"))
            if path.is_file()
        )
        versions = [migration.version for migration in migrations]
        if len(versions) != len(set(versions)):
            raise MigrationError(f"duplicate migration version in {root}")
        return cls(migrations)

    @property
    def versions(self) -> list[str]:
        return [migration.version for migration in self.migrations]

    def by_version(self) -> dict[str, Migration]:
        return {migration.version: migration for migration in self.migrations}


def split_sql_script(sql: str) -> list[str]:
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


def _serialize_applied_at(value: Any) -> str | None:
    if value is None:
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


class MigrationRunner:
    def __init__(
        self,
        engine: Engine,
        *,
        catalog: MigrationCatalog | None = None,
        migrations_dir: Path | None = None,
    ) -> None:
        self.engine = engine
        self.catalog = catalog or MigrationCatalog.from_directory(migrations_dir)

    @staticmethod
    def _migrations_table_exists(connection) -> bool:
        return "schema_migrations" in inspect(connection).get_table_names()

    @classmethod
    def _checksum_column_exists(cls, connection) -> bool:
        if not cls._migrations_table_exists(connection):
            return False
        return any(
            str(column["name"]) == "checksum"
            for column in inspect(connection).get_columns("schema_migrations")
        )

    @classmethod
    def _applied_rows(cls, connection) -> list[dict[str, Any]]:
        if not cls._migrations_table_exists(connection):
            return []
        has_checksum = cls._checksum_column_exists(connection)
        checksum_select = ", checksum" if has_checksum else ""
        rows = connection.execute(
            text(
                "SELECT version, applied_at"
                f"{checksum_select} FROM schema_migrations "
                "ORDER BY applied_at ASC, version ASC"
            )
        ).mappings().all()
        return [
            {
                "version": str(row["version"]),
                "applied_at": _serialize_applied_at(row["applied_at"]),
                "checksum": str(row["checksum"]) if has_checksum and row["checksum"] else None,
            }
            for row in rows
        ]

    @staticmethod
    def _acquire_lock(connection) -> None:
        if connection.dialect.name == "postgresql":
            connection.execute(
                text("SELECT pg_advisory_xact_lock(:lock_key)"),
                {"lock_key": _ADVISORY_LOCK_KEY},
            )

    @staticmethod
    def _checksum_mismatches(
        applied_rows: Iterable[dict[str, Any]],
        known: dict[str, Migration],
    ) -> list[dict[str, str]]:
        mismatches: list[dict[str, str]] = []
        for row in applied_rows:
            migration = known.get(str(row["version"]))
            actual = row.get("checksum")
            if migration is None or actual is None or actual == migration.checksum:
                continue
            mismatches.append(
                {
                    "version": migration.version,
                    "expected_checksum": migration.checksum,
                    "applied_checksum": str(actual),
                }
            )
        return mismatches

    def status(self) -> dict[str, object]:
        with self.engine.connect() as connection:
            table_exists = self._migrations_table_exists(connection)
            checksum_column_exists = self._checksum_column_exists(connection)
            applied_rows = self._applied_rows(connection)

        known = self.catalog.by_version()
        applied_versions = {str(row["version"]) for row in applied_rows}
        missing = [version for version in self.catalog.versions if version not in applied_versions]
        unexpected = sorted(version for version in applied_versions if version not in known)
        mismatches = self._checksum_mismatches(applied_rows, known)
        missing_checksums = sorted(
            str(row["version"])
            for row in applied_rows
            if str(row["version"]) in known and row.get("checksum") is None
        )
        enriched_rows = []
        for row in applied_rows:
            migration = known.get(str(row["version"]))
            expected = migration.checksum if migration else None
            enriched_rows.append(
                {
                    **row,
                    "expected_checksum": expected,
                    "checksum_valid": (
                        row.get("checksum") == expected
                        if row.get("checksum") is not None and expected is not None
                        else None
                    ),
                }
            )

        latest_applied = enriched_rows[-1]["version"] if enriched_rows else None
        is_current = bool(
            table_exists
            and checksum_column_exists
            and not missing
            and not unexpected
            and not mismatches
            and not missing_checksums
        )
        return {
            "dialect": self.engine.dialect.name,
            "schema_migrations_table_exists": table_exists,
            "migration_checksum_column_exists": checksum_column_exists,
            "known_migrations": self.catalog.versions,
            "applied_migrations": enriched_rows,
            "latest_applied_migration": latest_applied,
            "missing_migrations": missing,
            "unexpected_migrations": unexpected,
            "missing_checksums": missing_checksums,
            "checksum_mismatches": mismatches,
            "is_current": is_current,
        }

    def plan(self) -> list[dict[str, str | None]]:
        status = self.status()
        applied = {
            str(row["version"]): row
            for row in status.get("applied_migrations", [])
            if isinstance(row, dict)
        }
        plan: list[dict[str, str | None]] = []
        for migration in self.catalog.migrations:
            row = applied.get(migration.version)
            if row is None:
                state = "pending"
            elif row.get("checksum") is None:
                state = "applied_unverified"
            elif row.get("checksum") != migration.checksum:
                state = "checksum_mismatch"
            else:
                state = "applied"
            plan.append(
                {
                    "version": migration.version,
                    "checksum": migration.checksum,
                    "state": state,
                }
            )
        return plan

    def apply(self) -> list[dict[str, str]]:
        results: list[dict[str, str]] = []
        known = self.catalog.by_version()
        with self.engine.begin() as connection:
            self._acquire_lock(connection)
            applied_rows = self._applied_rows(connection)
            mismatches = self._checksum_mismatches(applied_rows, known)
            if mismatches:
                versions = ", ".join(item["version"] for item in mismatches)
                raise MigrationChecksumError(
                    f"refusing to apply migrations after checksum mismatch: {versions}"
                )
            applied_versions = {str(row["version"]) for row in applied_rows}

            for migration in self.catalog.migrations:
                if migration.version in applied_versions:
                    results.append({"version": migration.version, "status": "skipped"})
                    continue
                for statement in split_sql_script(migration.path.read_text(encoding="utf-8")):
                    if statement.strip().upper() in {"BEGIN", "BEGIN TRANSACTION", "COMMIT"}:
                        continue
                    connection.execute(text(statement))
                refreshed_rows = self._applied_rows(connection)
                refreshed_versions = {str(row["version"]) for row in refreshed_rows}
                if self._migrations_table_exists(connection) and migration.version not in refreshed_versions:
                    connection.execute(
                        text("INSERT INTO schema_migrations (version) VALUES (:version)"),
                        {"version": migration.version},
                    )
                    refreshed_versions.add(migration.version)
                applied_versions = refreshed_versions
                results.append({"version": migration.version, "status": "applied"})

            if self._checksum_column_exists(connection):
                for migration in self.catalog.migrations:
                    if migration.version not in applied_versions:
                        continue
                    connection.execute(
                        text(
                            "UPDATE schema_migrations SET checksum = :checksum "
                            "WHERE version = :version AND checksum IS NULL"
                        ),
                        {"version": migration.version, "checksum": migration.checksum},
                    )
        return results

    def verify(self) -> dict[str, object]:
        status = self.status()
        return {**status, "verified": bool(status["is_current"])}
