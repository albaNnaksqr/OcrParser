from __future__ import annotations

import os
import importlib
import subprocess
import sys
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

import ocr_platform.control.database as database
from ocr_platform.control.app import _LazyControlApp, create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.redaction import redact_database_url
from ocr_platform.control.settings import ControlSettings


ROOT = Path(__file__).resolve().parents[1]
control_app_module = importlib.import_module("ocr_platform.control.app")


def _session_factory(tmp_path, name: str = "control.db"):
    session_factory, engine = create_session_factory(
        f"sqlite:///{tmp_path / name}",
        settings=ControlSettings(
            database_url=f"sqlite:///{tmp_path / name}",
        ),
    )
    init_db(engine)
    return session_factory, engine


def test_control_settings_parse_supported_environment() -> None:
    settings = ControlSettings.from_environment(
        {
            "OCR_PLATFORM_DATABASE_URL": "sqlite:////tmp/control.db",
            "OCR_PLATFORM_REQUIRE_POSTGRES": "yes",
            "OCR_PLATFORM_AUTO_MIGRATE": "1",
            "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS": "true",
            "OCR_PLATFORM_HOST": "localhost",
            "OCR_PLATFORM_PORT": "18080",
            "OCR_PLATFORM_API_TOKEN": "control-secret",
            "OCR_PLATFORM_REQUIRE_API_TOKEN": "1",
            "OCR_PLATFORM_ENABLE_REMOTE_ADMIN": "true",
            "OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS": "yes",
        }
    )

    assert settings.database_url == "sqlite:////tmp/control.db"
    assert settings.require_postgres is True
    assert settings.auto_migrate is True
    assert settings.require_current_migrations is True
    assert settings.host == "localhost"
    assert settings.port == 18080
    assert settings.api_token == "control-secret"
    assert settings.require_api_token is True
    assert settings.enable_remote_admin is True
    assert settings.saved_model_profile_keys_allowed is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "invalid", ""])
def test_control_settings_preserve_false_for_nontruthy_values(value: str) -> None:
    settings = ControlSettings.from_environment(
        {
            "OCR_PLATFORM_REQUIRE_POSTGRES": value,
            "OCR_PLATFORM_AUTO_MIGRATE": value,
        }
    )

    assert settings.require_postgres is False
    assert settings.auto_migrate is False


@pytest.mark.parametrize("value", ["true", "yes", "on", "TRUE", "01"])
def test_auto_migrate_accepts_only_exact_one(value: str) -> None:
    settings = ControlSettings.from_environment(
        {"OCR_PLATFORM_AUTO_MIGRATE": value}
    )

    assert settings.auto_migrate is False


def test_saved_profile_key_disable_flag_takes_precedence() -> None:
    settings = ControlSettings.from_environment(
        {
            "OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS": "1",
            "OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS": "1",
        }
    )

    assert settings.saved_model_profile_keys_allowed is False


def test_control_settings_reject_invalid_port_without_reflecting_value() -> None:
    with pytest.raises(
        ValueError,
        match="OCR_PLATFORM_PORT must be an integer",
    ) as exc_info:
        ControlSettings.from_environment(
            {"OCR_PLATFORM_PORT": "invalid-port-value"}
        )

    assert "invalid-port-value" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_control_settings_are_frozen_and_secret_safe_in_repr() -> None:
    settings = ControlSettings(
        database_url="postgresql+psycopg://db-user:db-password@db/control",
        api_token="api-token-secret",
    )

    with pytest.raises(FrozenInstanceError):
        settings.port = 9999  # type: ignore[misc]

    rendered = repr(settings)
    assert "db-password" not in rendered
    assert "api-token-secret" not in rendered


def test_database_url_redaction_hides_password() -> None:
    rendered = redact_database_url(
        "postgresql+psycopg://db-user:db-password@db/control"
    )

    assert rendered == "postgresql+psycopg://db-user:***@db/control"
    assert "db-password" not in rendered


def test_invalid_database_url_error_does_not_expose_password() -> None:
    database_url = "sqlite://db-user:db-password@/tmp/control.db"
    settings = ControlSettings(database_url=database_url)

    with pytest.raises(
        RuntimeError,
        match="Invalid OCR Platform database URL",
    ) as exc_info:
        create_session_factory(settings=settings)

    assert "db-password" not in str(exc_info.value)
    assert "***" in str(exc_info.value)


def test_create_app_captures_environment_when_called(tmp_path, monkeypatch) -> None:
    session_factory, engine = _session_factory(tmp_path)
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "first-token")
    first_app = create_app(session_factory=session_factory)

    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "second-token")
    second_app = create_app(session_factory=session_factory)

    with TestClient(first_app) as first, TestClient(second_app) as second:
        assert first.get(
            "/api/servers",
            headers={"Authorization": "Bearer first-token"},
        ).status_code == 200
        assert first.get(
            "/api/servers",
            headers={"Authorization": "Bearer second-token"},
        ).status_code == 401
        assert second.get(
            "/api/servers",
            headers={"Authorization": "Bearer second-token"},
        ).status_code == 200
    engine.dispose()


def test_explicit_settings_are_not_overwritten_by_environment(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _session_factory(tmp_path)
    settings = ControlSettings(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        api_token="explicit-token",
        require_api_token=True,
    )
    app = create_app(
        session_factory=session_factory,
        settings=settings,
    )
    monkeypatch.setenv("OCR_PLATFORM_API_TOKEN", "environment-token")
    monkeypatch.setenv("OCR_PLATFORM_REQUIRE_API_TOKEN", "0")

    with TestClient(app) as client:
        assert client.get(
            "/api/servers",
            headers={"Authorization": "Bearer explicit-token"},
        ).status_code == 200
        assert client.get(
            "/api/servers",
            headers={"Authorization": "Bearer environment-token"},
        ).status_code == 401
        diagnostics = client.get(
            "/api/system/diagnostics",
            headers={"Authorization": "Bearer explicit-token"},
        ).json()

    assert diagnostics["api_auth"] == {
        "enabled": True,
        "required": True,
        "configured": True,
    }
    engine.dispose()


def test_explicit_database_settings_survive_environment_change(
    tmp_path,
    monkeypatch,
) -> None:
    selected_path = tmp_path / "selected.db"
    settings = ControlSettings(database_url=f"sqlite:///{selected_path}")
    monkeypatch.setenv(
        "OCR_PLATFORM_DATABASE_URL",
        f"sqlite:///{tmp_path / 'environment.db'}",
    )

    _, engine = create_session_factory(settings=settings)

    assert engine.url.database == str(selected_path)
    engine.dispose()


def test_readyz_never_reflects_arbitrary_exception_text(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _session_factory(tmp_path)
    app = create_app(session_factory=session_factory)

    def fail_diagnostics(*args, **kwargs):
        raise RuntimeError(
            "postgresql://user:db-password@db/control api-token-secret"
        )

    monkeypatch.setattr(
        "ocr_platform.control.domains.diagnostics.router.system_diagnostics",
        fail_diagnostics,
    )

    with TestClient(app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json() == {
        "ok": False,
        "service": "ocr-platform-control",
        "error": "Control diagnostics are unavailable.",
    }
    assert "db-password" not in response.text
    assert "api-token-secret" not in response.text
    engine.dispose()


def test_default_and_injected_apps_share_database_startup_policy(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = create_session_factory(
        f"sqlite:///{tmp_path / 'control.db'}"
    )
    settings = ControlSettings(
        database_url=f"sqlite:///{tmp_path / 'control.db'}",
        auto_migrate=True,
        require_current_migrations=True,
    )
    calls = []

    def record_init(bind=None, *, settings=None):
        selected_bind = bind or engine
        calls.append(("init", selected_bind, settings))
        monkeypatch.setattr(database, "engine", selected_bind)

    def record_validation(bind, *, settings=None):
        calls.append(("validate", bind, settings))

    def record_bootstrap(factory, bind):
        calls.append(("bootstrap", factory, bind))

    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    monkeypatch.setattr(database, "init_db", record_init)
    monkeypatch.setattr(
        control_app_module,
        "validate_current_migrations",
        record_validation,
    )
    monkeypatch.setattr(
        control_app_module,
        "bootstrap_control_database",
        record_bootstrap,
    )

    injected_app = create_app(
        session_factory=session_factory,
        settings=settings,
    )
    with TestClient(injected_app):
        pass
    default_app = create_app(settings=settings)
    with TestClient(default_app):
        pass

    assert calls == [
        ("init", engine, settings),
        ("validate", engine, settings),
        ("bootstrap", session_factory, engine),
        ("init", engine, settings),
        ("validate", engine, settings),
        ("bootstrap", session_factory, engine),
    ]
    engine.dispose()


@pytest.mark.parametrize(
    ("auto_migrate", "expected_calls"),
    [
        (False, []),
        (True, ["runner", "status", "apply"]),
    ],
)
def test_injected_postgres_factory_obeys_auto_migration_policy(
    monkeypatch,
    auto_migrate,
    expected_calls,
) -> None:
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    calls = []

    class BoundSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def get_bind(self):
            return engine

    class RunnerStub:
        def __init__(self, bind):
            assert bind is engine
            calls.append("runner")

        def status(self):
            calls.append("status")
            return {
                "unexpected_migrations": [],
                "checksum_mismatches": [],
            }

        def apply(self):
            calls.append("apply")

    monkeypatch.setattr(database, "MigrationRunner", RunnerStub)
    monkeypatch.setattr(
        database.Base.metadata,
        "create_all",
        lambda bind: calls.append("create"),
    )
    monkeypatch.setattr(
        database,
        "_ensure_compatible_schema",
        lambda bind: calls.append("compat"),
    )
    monkeypatch.setattr(
        database,
        "_ensure_production_indexes",
        lambda bind: calls.append("indexes"),
    )
    monkeypatch.setattr(
        control_app_module,
        "bootstrap_control_database",
        lambda factory, bind: None,
    )

    create_app(
        session_factory=BoundSession,
        settings=ControlSettings(auto_migrate=auto_migrate),
    )

    assert calls == expected_calls


def test_lazy_asgi_app_defers_factory_until_call_or_attribute_access(
    tmp_path,
) -> None:
    session_factory, engine = _session_factory(tmp_path)
    application = create_app(session_factory=session_factory)
    created = []
    lazy = _LazyControlApp(
        lambda: created.append(application) or application
    )

    assert created == []
    assert "unresolved" in repr(lazy)
    assert lazy.openapi()["info"]["title"] == "OCR Platform Control API"
    assert created == [application]
    assert lazy.routes is application.routes
    with TestClient(lazy) as client:
        assert client.get("/healthz").status_code == 200
    assert created == [application]
    engine.dispose()


def test_uvicorn_compatibility_app_import_does_not_parse_environment() -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    environment["OCR_PLATFORM_REQUIRE_API_TOKEN"] = "1"
    environment["OCR_PLATFORM_PORT"] = "not-an-integer"
    environment.pop("OCR_PLATFORM_API_TOKEN", None)
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from uvicorn.importer import import_from_string; "
                "application = import_from_string("
                "'ocr_platform.control.app:app'); "
                "assert callable(application); "
                "print(repr(application))"
            ),
        ],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "unresolved" in completed.stdout


def test_global_database_rejects_explicit_settings_url_mismatch(
    tmp_path,
    monkeypatch,
) -> None:
    first_url = f"sqlite:///{tmp_path / 'first-sensitive.db'}"
    second_url = f"sqlite:///{tmp_path / 'second-sensitive.db'}"
    session_factory, engine = create_session_factory(
        settings=ControlSettings(database_url=first_url)
    )
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    monkeypatch.setattr(database, "engine", engine)

    with pytest.raises(
        RuntimeError,
        match="already configured for a different database URL",
    ) as exc_info:
        next(
            database.get_session(
                settings=ControlSettings(database_url=second_url)
            )
        )

    message = str(exc_info.value)
    assert "first-sensitive.db" not in message
    assert "second-sensitive.db" not in message
    engine.dispose()


def test_global_database_accepts_matching_explicit_settings(
    tmp_path,
    monkeypatch,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'matching.db'}"
    settings = ControlSettings(database_url=database_url)
    session_factory, engine = create_session_factory(settings=settings)
    monkeypatch.setattr(database, "SessionLocal", session_factory)
    monkeypatch.setattr(database, "engine", engine)

    session_generator = database.get_session(settings=settings)
    session = next(session_generator)
    session.close()
    session_generator.close()

    engine.dispose()


def test_no_arg_database_calls_reuse_explicitly_configured_engine(
    tmp_path,
    monkeypatch,
) -> None:
    first_url = f"sqlite:///{tmp_path / 'first-sensitive.db'}"
    second_url = f"sqlite:///{tmp_path / 'second-sensitive.db'}"
    monkeypatch.setattr(database, "SessionLocal", None)
    monkeypatch.setattr(database, "engine", None)
    monkeypatch.setattr(database, "_configured_database_url", None)
    monkeypatch.setattr(database, "_configured_database_source", None)

    _, engine = database.configure_database(first_url)
    database.init_db()
    session_generator = database.get_session()
    session = next(session_generator)
    session.close()
    session_generator.close()

    assert database._configured_database_url == engine.url
    assert database._configured_database_source == "database_url"
    with pytest.raises(
        RuntimeError,
        match="already configured for a different database URL",
    ) as exc_info:
        next(
            database.get_session(
                settings=ControlSettings(database_url=second_url)
            )
        )

    message = str(exc_info.value)
    assert "first-sensitive.db" not in message
    assert "second-sensitive.db" not in message
    engine.dispose()


def test_explicit_remote_admin_settings_override_environment(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _session_factory(tmp_path)
    monkeypatch.setenv("OCR_PLATFORM_ENABLE_REMOTE_ADMIN", "1")
    disabled_app = create_app(
        session_factory=session_factory,
        settings=ControlSettings(enable_remote_admin=False),
    )
    monkeypatch.delenv("OCR_PLATFORM_ENABLE_REMOTE_ADMIN", raising=False)
    enabled_app = create_app(
        session_factory=session_factory,
        settings=ControlSettings(enable_remote_admin=True),
    )

    with TestClient(disabled_app) as disabled:
        assert disabled.get("/api/remote-workers/targets").status_code == 403
    with TestClient(enabled_app) as enabled:
        assert enabled.get("/api/remote-workers/targets").status_code == 200
    engine.dispose()


def test_explicit_saved_key_policy_overrides_environment(
    tmp_path,
    monkeypatch,
) -> None:
    session_factory, engine = _session_factory(tmp_path)
    request = {
        "label": "DotsOCR explicit settings",
        "engine": "dotsocr",
        "requires_api_key": True,
        "api_key": "runtime-test-secret",
    }
    monkeypatch.setenv("OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS", "1")
    disabled_app = create_app(
        session_factory=session_factory,
        settings=ControlSettings(
            saved_model_profile_keys_allowed=False,
        ),
    )
    monkeypatch.delenv(
        "OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS",
        raising=False,
    )
    monkeypatch.setenv("OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS", "1")
    enabled_app = create_app(
        session_factory=session_factory,
        settings=ControlSettings(
            saved_model_profile_keys_allowed=True,
        ),
    )

    with TestClient(disabled_app) as disabled:
        rejected = disabled.put(
            "/api/model-profiles/dotsocr_15",
            json=request,
        )
        assert rejected.status_code == 400
    with TestClient(enabled_app) as enabled:
        accepted = enabled.put(
            "/api/model-profiles/dotsocr_15",
            json=request,
        )
        assert accepted.status_code == 200
        assert accepted.json()["has_api_key"] is True
    engine.dispose()
