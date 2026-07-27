from __future__ import annotations

import concurrent.futures
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import ocr_platform.control.database as database
from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.domains.diagnostics.queries import system_diagnostics
from ocr_platform.control.readiness import (
    DatabaseReadinessProbe,
    MIGRATION_COMMANDS,
    readiness_from_database_status,
)
from ocr_platform.control.settings import ControlSettings


def _database_status(
    *,
    dialect: str = "postgresql",
    table_exists: bool = True,
    checksum_column_exists: bool = True,
    missing_migrations: list[str] | None = None,
    unexpected_migrations: list[str] | None = None,
    missing_checksums: list[str] | None = None,
    checksum_mismatches: list[dict[str, str]] | None = None,
    is_current: bool = True,
) -> dict[str, object]:
    return {
        "dialect": dialect,
        "schema_migrations_table_exists": table_exists,
        "migration_checksum_column_exists": checksum_column_exists,
        "known_migrations": ["public-migration"],
        "applied_migrations": [],
        "latest_applied_migration": None,
        "missing_migrations": missing_migrations or [],
        "unexpected_migrations": unexpected_migrations or [],
        "missing_checksums": missing_checksums or [],
        "checksum_mismatches": checksum_mismatches or [],
        "is_current": is_current,
    }


def _client(tmp_path, *, api_token: str | None = None):
    database_url = f"sqlite:///{tmp_path / 'control.db'}"
    session_factory, engine = create_session_factory(database_url)
    init_db(engine)
    app = create_app(
        session_factory=session_factory,
        settings=ControlSettings(
            database_url=database_url,
            api_token=api_token,
        ),
    )
    return TestClient(app), app, engine


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        (
            {
                "unexpected_migrations": ["private-version"],
                "is_current": False,
            },
            "unexpected_migrations",
        ),
        (
            {
                "checksum_mismatches": [
                    {
                        "version": "private-version",
                        "expected_checksum": "private-expected",
                        "applied_checksum": "private-actual",
                    }
                ],
                "is_current": False,
            },
            "checksum_mismatch",
        ),
        (
            {
                "table_exists": False,
                "checksum_column_exists": False,
                "is_current": False,
            },
            "migration_table_missing",
        ),
        (
            {
                "checksum_column_exists": False,
                "is_current": False,
            },
            "checksum_column_missing",
        ),
        (
            {
                "missing_checksums": ["private-version"],
                "is_current": False,
            },
            "migration_checksums_missing",
        ),
        (
            {
                "missing_migrations": ["private-version"],
                "is_current": False,
            },
            "migrations_pending",
        ),
        ({"is_current": False}, "migration_state_unknown"),
    ],
)
def test_postgres_readiness_reasons_are_fixed_categories(
    overrides,
    reason,
) -> None:
    readiness = readiness_from_database_status(
        _database_status(**overrides)
    )

    assert readiness.ready is False
    assert readiness.reason == reason
    assert "private" not in reason


def test_sqlite_development_database_is_ready_even_without_migrations() -> None:
    readiness = readiness_from_database_status(
        _database_status(
            dialect="sqlite",
            table_exists=False,
            checksum_column_exists=False,
            missing_migrations=["all"],
            is_current=False,
        )
    )

    assert readiness.ready is True
    assert readiness.reason is None


def test_probe_requires_and_uses_explicit_database_binds(
    tmp_path,
    monkeypatch,
) -> None:
    _, engine = create_session_factory(
        f"sqlite:///{tmp_path / 'control.db'}"
    )
    captured = []
    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda bind: captured.append(bind)
        or _database_status(dialect="sqlite"),
    )

    class BoundSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def get_bind(self):
            return engine

    assert DatabaseReadinessProbe(BoundSession).check().ready is True
    assert DatabaseReadinessProbe(
        bind_provider=lambda: engine
    ).check().ready is True
    with pytest.raises(
        ValueError,
        match="requires an explicit session_factory or bind_provider",
    ):
        DatabaseReadinessProbe()
    assert captured == [engine, engine]
    engine.dispose()


def test_probe_caches_only_ready_and_force_bypasses_cache(monkeypatch) -> None:
    now = [0.0]
    statuses = [
        _database_status(),
        _database_status(
            missing_migrations=["pending"],
            is_current=False,
        ),
        _database_status(
            missing_migrations=["pending"],
            is_current=False,
        ),
    ]
    calls = []
    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda bind: calls.append(bind) or statuses.pop(0),
    )
    probe = DatabaseReadinessProbe(
        bind_provider=lambda: "bind",
        clock=lambda: now[0],
        ready_ttl_seconds=1.0,
    )

    assert probe.check().ready is True
    now[0] = 0.5
    assert probe.check().ready is True
    assert len(calls) == 1
    assert probe.check(force=True).ready is False
    assert probe.check().ready is False
    assert len(calls) == 3


def test_probe_serializes_concurrent_ready_refresh(monkeypatch) -> None:
    started = threading.Event()
    release = threading.Event()
    calls = []

    def describe(bind):
        calls.append(bind)
        started.set()
        assert release.wait(timeout=5)
        return _database_status()

    monkeypatch.setattr(database, "describe_database_status", describe)
    probe = DatabaseReadinessProbe(bind_provider=lambda: "bind")
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(probe.check) for _ in range(8)]
        assert started.wait(timeout=5)
        release.set()
        results = [future.result(timeout=5) for future in futures]

    assert all(result.ready for result in results)
    assert calls == ["bind"]


def test_probe_is_read_only_and_never_uses_schema_mutation_paths(
    monkeypatch,
) -> None:
    def forbidden(*args, **kwargs):
        raise AssertionError("readiness probe attempted schema mutation")

    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda bind: _database_status(),
    )
    monkeypatch.setattr(database.Base.metadata, "create_all", forbidden)
    monkeypatch.setattr(database, "_ensure_compatible_schema", forbidden)
    monkeypatch.setattr(database, "_ensure_production_indexes", forbidden)
    monkeypatch.setattr(database.MigrationRunner, "apply", forbidden)

    assert DatabaseReadinessProbe(
        bind_provider=lambda: object()
    ).check().ready is True


def test_authentication_precedes_readiness_and_skips_probe(
    tmp_path,
    monkeypatch,
) -> None:
    client, app, engine = _client(tmp_path, api_token="control-token")
    calls = []
    monkeypatch.setattr(
        app.state.database_readiness_probe,
        "check",
        lambda **kwargs: calls.append(kwargs)
        or readiness_from_database_status(
            _database_status(
                missing_migrations=["private-version"],
                is_current=False,
            )
        ),
    )

    assert client.get("/api/servers").status_code == 401
    assert client.get(
        "/api/servers",
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401
    assert calls == []

    response = client.get(
        "/api/servers",
        headers={"Authorization": "Bearer control-token"},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == {
        "code": "control_database_not_ready",
        "reason": "migrations_pending",
        "commands": MIGRATION_COMMANDS,
    }
    assert len(calls) == 1
    engine.dispose()


def test_drift_keeps_health_and_non_api_surface_available(
    tmp_path,
    monkeypatch,
) -> None:
    client, app, engine = _client(tmp_path)
    monkeypatch.setattr(
        app.state.database_readiness_probe,
        "check",
        lambda **kwargs: readiness_from_database_status(
            _database_status(table_exists=False, is_current=False)
        ),
    )

    assert client.get("/healthz").status_code == 200
    ready = client.get("/readyz")
    assert ready.status_code == 503
    assert ready.json() == {
        "ok": False,
        "service": "ocr-platform-control",
        "code": "control_database_not_ready",
        "reason": "migration_table_missing",
        "commands": MIGRATION_COMMANDS,
    }
    assert client.get("/").status_code in {200, 307}
    assert client.get("/ui/").status_code == 200
    assert client.head("/ui/main.js").status_code == 200
    assert client.get("/openapi.json").status_code == 200
    assert client.get("/docs").status_code == 200
    assert client.get("/not-a-real-path").status_code == 404
    engine.dispose()


def test_readiness_allowlist_is_exact_and_preserves_existing_redirects(
    tmp_path,
    monkeypatch,
) -> None:
    client, app, engine = _client(tmp_path, api_token="control-token")
    monkeypatch.setattr(
        app.state.database_readiness_probe,
        "check",
        lambda **kwargs: readiness_from_database_status(
            _database_status(
                missing_migrations=["pending"],
                is_current=False,
            )
        ),
    )
    auth = {"Authorization": "Bearer control-token"}

    assert client.get(
        "/api/system/database/",
        headers=auth,
        follow_redirects=False,
    ).status_code == 307
    assert client.get(
        "/api/servers/",
        headers=auth,
        follow_redirects=False,
    ).status_code == 307
    assert client.get(
        "/api/servers/",
        headers=auth,
    ).status_code == 503
    assert client.get(
        "/api/system-not-allowlisted",
        headers=auth,
    ).status_code == 503
    assert client.get(
        "/api/not-a-real-path/",
        headers=auth,
        follow_redirects=False,
    ).status_code == 503
    assert client.head("/api/servers", headers=auth).status_code == 503
    engine.dispose()


def test_same_app_recovers_immediately_after_external_migration(
    tmp_path,
    monkeypatch,
) -> None:
    client, app, engine = _client(tmp_path)
    status = {
        "value": _database_status(
            missing_migrations=["pending"],
            is_current=False,
        )
    }
    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda bind: status["value"],
    )

    assert client.get("/api/servers").status_code == 503
    status["value"] = _database_status()
    assert client.get("/api/servers").status_code == 200
    assert client.get("/readyz").status_code == 200
    engine.dispose()


def test_readiness_and_diagnostic_failures_never_reflect_exception_text(
    tmp_path,
    monkeypatch,
) -> None:
    client, app, engine = _client(tmp_path, api_token="control-token")
    secret = "postgresql://user:private-password@db/control private-token"

    def fail_status(bind):
        raise RuntimeError(secret)

    monkeypatch.setattr(database, "describe_database_status", fail_status)
    auth = {"Authorization": "Bearer control-token"}

    ready = client.get("/readyz")
    business = client.get("/api/servers", headers=auth)
    database_response = client.get("/api/system/database", headers=auth)
    diagnostics = client.get("/api/system/diagnostics", headers=auth)
    assert ready.status_code == 503
    assert business.status_code == 503
    assert database_response.status_code == 503
    assert diagnostics.status_code == 503
    for response in (ready, business, database_response, diagnostics):
        assert "private-password" not in response.text
        assert "private-token" not in response.text
    assert database_response.json() == {
        "detail": {
            "code": "control_database_status_unavailable",
            "reason": "database_status_unavailable",
            "commands": MIGRATION_COMMANDS,
        }
    }
    engine.dispose()


def test_worker_diagnostic_failure_is_fixed_and_sanitized(monkeypatch) -> None:
    session = SimpleNamespace(get_bind=lambda: object())
    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda bind: _database_status(table_exists=False, is_current=False),
    )
    monkeypatch.setattr(
        "ocr_platform.control.domains.diagnostics.queries.worker_diagnostics",
        lambda session: (_ for _ in ()).throw(
            RuntimeError("private-worker-query")
        ),
    )

    payload = system_diagnostics(session)

    assert payload["workers"] == {
        "total": 0,
        "ready": 0,
        "stale": 0,
        "with_shared_roots": 0,
        "resource_constrained": 0,
    }
    issue = next(
        item
        for item in payload["issues"]
        if item["code"] == "worker_diagnostics_unavailable"
    )
    assert issue == {
        "severity": "error",
        "code": "worker_diagnostics_unavailable",
        "message": "Worker diagnostics are unavailable.",
    }
    assert "private-worker-query" not in str(payload)


def test_allowlisted_diagnostics_are_authenticated_and_safe_on_fresh_schema(
    tmp_path,
    monkeypatch,
) -> None:
    client, app, engine = _client(tmp_path, api_token="control-token")
    fresh_status = _database_status(
        table_exists=False,
        checksum_column_exists=False,
        missing_migrations=["pending"],
        is_current=False,
    )
    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda bind: fresh_status,
    )
    monkeypatch.setattr(
        "ocr_platform.control.domains.diagnostics.queries.worker_diagnostics",
        lambda session: (_ for _ in ()).throw(
            RuntimeError("private-worker-query")
        ),
    )

    assert client.get("/api/system/database").status_code == 401
    assert client.get("/api/system/diagnostics").status_code == 401
    auth = {"Authorization": "Bearer control-token"}
    database_response = client.get("/api/system/database", headers=auth)
    diagnostics = client.get("/api/system/diagnostics", headers=auth)

    assert database_response.status_code == 200
    assert database_response.json()["schema_migrations_table_exists"] is False
    assert diagnostics.status_code == 200
    payload = diagnostics.json()
    assert any(
        issue["code"] == "worker_diagnostics_unavailable"
        for issue in payload["issues"]
    )
    assert "private-worker-query" not in diagnostics.text
    engine.dispose()
