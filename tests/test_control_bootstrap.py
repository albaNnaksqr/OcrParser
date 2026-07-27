from __future__ import annotations

import ast
import concurrent.futures
import os
import threading
from pathlib import Path

import pytest
from sqlalchemy import delete, event, func, select
from sqlalchemy.orm import Session

import ocr_platform.control.database as database
from ocr_platform.control.app import create_app
from ocr_platform.control.bootstrap import (
    bootstrap_control_database,
    seed_default_model_profiles,
)
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.domains.common import DEFAULT_MODEL_PROFILES
from ocr_platform.control.domains.model_profiles.core import (
    ensure_default_model_profiles,
    list_model_profiles,
)
from ocr_platform.control.models import (
    ModelProfile,
    ModelProfileCertification,
)


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = os.environ.get("OCR_TEST_POSTGRES_URL")


def _custom_dotsocr() -> ModelProfile:
    return ModelProfile(
        id="dotsocr_15",
        label="User customized DotsOCR",
        engine="dotsocr",
        ip="model.example",
        port=31000,
        model_name="CustomDotsOCR",
        is_default=True,
    )


def _assert_default_rows(session) -> None:
    profiles = session.execute(
        select(ModelProfile)
        .where(ModelProfile.id.in_(DEFAULT_MODEL_PROFILES))
        .order_by(ModelProfile.id)
    ).scalars().all()
    assert [profile.id for profile in profiles] == sorted(
        DEFAULT_MODEL_PROFILES
    )
    assert len({profile.id for profile in profiles}) == len(
        DEFAULT_MODEL_PROFILES
    )
    dotsocr = session.get(ModelProfile, "dotsocr_15")
    assert dotsocr.label == "User customized DotsOCR"
    assert dotsocr.ip == "model.example"
    assert dotsocr.port == 31000
    assert session.scalar(
        select(func.count()).select_from(ModelProfileCertification)
    ) == 0


def _run_concurrent_seed(
    session_factory,
    *,
    workers: int = 8,
) -> list[int]:
    barrier = threading.Barrier(workers)

    def seed() -> int:
        with session_factory() as session:
            barrier.wait(timeout=10)
            return seed_default_model_profiles(session)

    with concurrent.futures.ThreadPoolExecutor(
        max_workers=workers
    ) as executor:
        return [
            future.result(timeout=30)
            for future in [
                executor.submit(seed) for _ in range(workers)
            ]
        ]


def test_sqlite_bootstrap_is_concurrent_idempotent_and_non_overwriting(
    tmp_path,
) -> None:
    session_factory, engine = create_session_factory(
        f"sqlite:///{tmp_path / 'control.db'}"
    )
    init_db(engine)
    with session_factory() as session:
        session.add(_custom_dotsocr())
        session.commit()

    inserted = _run_concurrent_seed(session_factory)

    assert sum(inserted) == len(DEFAULT_MODEL_PROFILES) - 1
    with session_factory() as session:
        _assert_default_rows(session)
        ensure_default_model_profiles(session)
        _assert_default_rows(session)
    engine.dispose()


@pytest.mark.skipif(
    not POSTGRES_URL,
    reason="OCR_TEST_POSTGRES_URL is required for PostgreSQL bootstrap tests",
)
def test_postgres_bootstrap_is_concurrent_idempotent_and_non_overwriting():
    session_factory, engine = create_session_factory(POSTGRES_URL)
    try:
        with session_factory() as session:
            session.execute(
                delete(ModelProfileCertification).where(
                    ModelProfileCertification.profile_id.in_(
                        DEFAULT_MODEL_PROFILES
                    )
                )
            )
            session.execute(
                delete(ModelProfile).where(
                    ModelProfile.id.in_(DEFAULT_MODEL_PROFILES)
                )
            )
            session.add(_custom_dotsocr())
            session.commit()

        inserted = _run_concurrent_seed(session_factory)

        assert sum(inserted) == len(DEFAULT_MODEL_PROFILES) - 1
        with session_factory() as session:
            _assert_default_rows(session)
    finally:
        with session_factory() as session:
            session.execute(
                delete(ModelProfileCertification).where(
                    ModelProfileCertification.profile_id.in_(
                        DEFAULT_MODEL_PROFILES
                    )
                )
            )
            session.execute(
                delete(ModelProfile).where(
                    ModelProfile.id.in_(DEFAULT_MODEL_PROFILES)
                )
            )
            session.commit()
        engine.dispose()


def test_current_sqlite_startup_seeds_defaults_without_certifications(
    tmp_path,
) -> None:
    database_url = f"sqlite:///{tmp_path / 'control.db'}"
    session_factory, engine = create_session_factory(database_url)
    init_db(engine)

    create_app(session_factory=session_factory)

    with session_factory() as session:
        assert session.scalar(
            select(func.count()).select_from(ModelProfile)
        ) == len(DEFAULT_MODEL_PROFILES)
        assert session.scalar(
            select(func.count()).select_from(ModelProfileCertification)
        ) == 0
    engine.dispose()


def test_model_profile_list_is_strictly_read_only_and_does_not_seed(
    tmp_path,
) -> None:
    session_factory, engine = create_session_factory(
        f"sqlite:///{tmp_path / 'control.db'}"
    )
    init_db(engine)
    statements = []
    commits = []
    flushes = []

    with session_factory() as session:
        event.listen(
            engine,
            "before_cursor_execute",
            lambda conn, cursor, statement, parameters, context, executemany:
            statements.append(statement),
        )
        event.listen(session, "after_commit", lambda current: commits.append(1))
        event.listen(
            session,
            "before_flush",
            lambda current, context, instances: flushes.append(1),
        )

        assert list_model_profiles(session) == []

    assert statements
    assert all(statement.lstrip().upper().startswith("SELECT") for statement in statements)
    assert commits == []
    assert flushes == []
    engine.dispose()


def test_http_model_profile_list_does_not_reseed_or_commit(
    tmp_path,
) -> None:
    from fastapi.testclient import TestClient

    database_url = f"sqlite:///{tmp_path / 'control.db'}"
    session_factory, engine = create_session_factory(database_url)
    init_db(engine)
    app = create_app(session_factory=session_factory)
    with session_factory() as session:
        session.execute(delete(ModelProfile))
        session.commit()

    statements = []
    commits = []
    flushes = []

    def record_statement(
        conn,
        cursor,
        statement,
        parameters,
        context,
        executemany,
    ):
        statements.append(statement)

    def record_commit(session):
        commits.append(1)

    def record_flush(session, context, instances):
        flushes.append(1)

    event.listen(engine, "before_cursor_execute", record_statement)
    event.listen(Session, "after_commit", record_commit)
    event.listen(Session, "before_flush", record_flush)
    try:
        response = TestClient(app).get("/api/model-profiles")
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
        event.remove(Session, "after_commit", record_commit)
        event.remove(Session, "before_flush", record_flush)

    assert response.status_code == 200
    assert response.json() == []
    normalized = [statement.lstrip().upper() for statement in statements]
    assert not any(
        statement.startswith(("INSERT", "UPDATE", "DELETE"))
        for statement in normalized
    )
    assert commits == []
    assert flushes == []
    engine.dispose()


@pytest.mark.parametrize(
    ("status_or_error", "expected_status"),
    [
        (
            {
                "dialect": "postgresql",
                "is_current": False,
            },
            "schema_not_current",
        ),
        (RuntimeError("private database error"), "database_unavailable"),
    ],
)
def test_postgres_drift_or_unavailable_skips_seed_without_opening_session(
    monkeypatch,
    status_or_error,
    expected_status,
) -> None:
    engine = type(
        "Engine",
        (),
        {"dialect": type("Dialect", (), {"name": "postgresql"})()},
    )()
    opened = []

    def status(bind):
        if isinstance(status_or_error, Exception):
            raise status_or_error
        return status_or_error

    monkeypatch.setattr(database, "describe_database_status", status)

    result = bootstrap_control_database(
        lambda: opened.append(True),
        engine,
    )

    assert result.status == expected_status
    assert result.inserted_profiles == 0
    assert opened == []


def test_current_schema_seed_failure_is_not_silenced(monkeypatch) -> None:
    engine = type(
        "Engine",
        (),
        {"dialect": type("Dialect", (), {"name": "postgresql"})()},
    )()
    monkeypatch.setattr(
        database,
        "describe_database_status",
        lambda bind: {"dialect": "postgresql", "is_current": True},
    )

    class FailingFactory:
        def __call__(self):
            raise RuntimeError("seed failed")

    with pytest.raises(
        RuntimeError,
        match="^Control database bootstrap failed\\.$",
    ) as exc_info:
        bootstrap_control_database(FailingFactory(), engine)

    assert "seed failed" not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_production_profile_paths_do_not_call_legacy_lazy_seed() -> None:
    files = [
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "model_profiles"
        / "core.py",
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "workers"
        / "core.py",
    ]
    calls = []
    definitions = []
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        definitions.extend(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "ensure_default_model_profiles"
        )
        calls.extend(
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "ensure_default_model_profiles"
        )

    assert len(definitions) == 2
    assert calls == []
