from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.domains.manifests import commands as manifest_commands
from ocr_platform.control.domains.manifests import construction as manifest_construction
from ocr_platform.control.migration import MigrationCatalog, MigrationRunner
from ocr_platform.control.models import (
    Job,
    Manifest,
    ModelProfile,
    ModelProfileCertification,
    Server,
    WorkShard,
)
from ocr_platform.control.schemas import (
    RemoteManifestRegisterRequest,
    RemoteManifestShardRequest,
)
from ocr_platform.control.settings import ControlSettings


POSTGRES_URL = os.environ.get("OCR_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="OCR_TEST_POSTGRES_URL is required for PostgreSQL migration bridge tests",
)


def test_postgres_certification_defaults_checks_and_cascade():
    _, engine = create_session_factory(POSTGRES_URL)
    profile_id = f"bridge-{uuid.uuid4()}"
    explicit_certified_at = datetime(
        2026,
        7,
        27,
        1,
        2,
        3,
        tzinfo=timezone(timedelta(hours=-4)),
    )
    try:
        with engine.connect() as connection, Session(bind=connection) as session:
            session.execute(text("SET TIME ZONE 'Asia/Shanghai'"))
            profile = ModelProfile(
                id=profile_id,
                label="Migration bridge test",
                engine="dotsocr",
            )
            profile.certification = ModelProfileCertification(
                certified_at=explicit_certified_at,
            )
            session.add(profile)
            session.commit()

            certification = session.get(ModelProfileCertification, profile_id)
            assert certification is not None
            assert certification.enforcement == "off"
            assert certification.status == "contract_only"
            assert certification.risk_acceptance_json == "{}"
            assert certification.certified_at is not None
            assert certification.certified_at.tzinfo is not None
            assert certification.certified_at.astimezone(timezone.utc) == (
                explicit_certified_at.astimezone(timezone.utc)
            )
            assert certification.updated_at is not None
            assert certification.updated_at.tzinfo is not None
            first_updated_at = certification.updated_at.astimezone(timezone.utc)
            assert abs(
                (datetime.now(timezone.utc) - first_updated_at).total_seconds()
            ) < 30

            certification.status = "verified"
            session.commit()
            session.refresh(certification)
            assert certification.updated_at.tzinfo is not None
            assert certification.updated_at.astimezone(timezone.utc) >= first_updated_at

            certification.enforcement = "invalid"
            with pytest.raises(IntegrityError):
                session.commit()
            session.rollback()

            profile = session.get(ModelProfile, profile_id)
            assert profile is not None
            session.delete(profile)
            session.commit()
            assert session.get(ModelProfileCertification, profile_id) is None
    finally:
        engine.dispose()


def test_postgres_0019_catalog_rejects_0020_as_unexpected():
    _, engine = create_session_factory(POSTGRES_URL)
    try:
        current_catalog = MigrationCatalog.from_directory()
        older_catalog = MigrationCatalog(
            tuple(
                migration
                for migration in current_catalog.migrations
                if migration.version != "0020_model_profile_certification"
            )
        )

        current_status = MigrationRunner(engine, catalog=current_catalog).status()
        older_status = MigrationRunner(engine, catalog=older_catalog).status()

        assert current_status["is_current"] is True
        assert older_status["is_current"] is False
        assert older_status["unexpected_migrations"] == [
            "0020_model_profile_certification"
        ]
    finally:
        engine.dispose()


def test_postgres_startup_migration_policy_accepts_explicit_opt_in():
    _, engine = create_session_factory(POSTGRES_URL)
    settings = ControlSettings(
        database_url=POSTGRES_URL,
        auto_migrate=True,
        require_current_migrations=True,
    )
    try:
        before = MigrationRunner(engine).status()
        assert before["is_current"] is True

        init_db(engine, settings=settings)

        after = MigrationRunner(engine).verify()
        assert after["verified"] is True
        assert after["applied_migrations"] == before["applied_migrations"]
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    ("shard_count", "expected_error"),
    [
        (2, "job already has registered shards"),
        (0, "job already has registered manifest"),
    ],
)
def test_postgres_concurrent_remote_manifest_registration_is_serialized(
    shard_count: int,
    expected_error: str,
    monkeypatch,
):
    session_factory, engine = create_session_factory(POSTGRES_URL)
    suffix = uuid.uuid4().hex[:20]
    server_id = f"ms-{suffix}"
    job_id = f"mj-{suffix}"
    first_lock_acquired = threading.Event()
    release_first = threading.Event()
    second_backend_ready = threading.Event()
    first_has_static_shards_call = True
    has_static_shards_call_lock = threading.Lock()
    second_backend_pid: list[int] = []
    original_has_static_shards = manifest_construction.has_static_shards

    def hold_first_registration_after_job_lock(session, current_job_id):
        nonlocal first_has_static_shards_call
        with has_static_shards_call_lock:
            should_hold = first_has_static_shards_call
            first_has_static_shards_call = False
        if should_hold:
            first_lock_acquired.set()
            if not release_first.wait(timeout=15):
                raise TimeoutError("timed out waiting to release first registration")
        return original_has_static_shards(session, current_job_id)

    monkeypatch.setattr(
        manifest_construction,
        "has_static_shards",
        hold_first_registration_after_job_lock,
    )

    def request(worker: str) -> RemoteManifestRegisterRequest:
        root = f"/shared/manifests/{job_id}/{worker}"
        return RemoteManifestRegisterRequest(
            input_mode="remote_folder_snapshot",
            input_root="/shared/input",
            manifest_path=f"{root}/manifest.jsonl",
            meta_path=f"{root}/manifest.meta.json",
            file_count=shard_count,
            total_bytes=12,
            shards=[
                RemoteManifestShardRequest(
                    shard_index=index,
                    shard_path=(
                        f"{root}/shards/shard-{index:06d}.jsonl"
                    ),
                    file_count=1,
                )
                for index in range(1, shard_count + 1)
            ],
        )

    def register(
        worker: str,
        *,
        observe_backend: bool = False,
    ) -> tuple[str, str]:
        with engine.connect() as connection, Session(bind=connection) as session:
            if observe_backend:
                session.execute(text("SET SESSION lock_timeout = '5s'"))
                session.execute(text("SET SESSION statement_timeout = '15s'"))
                pid = session.scalar(text("SELECT pg_backend_pid()"))
                assert pid is not None
                session.commit()
                second_backend_pid.append(int(pid))
                second_backend_ready.set()
            try:
                manifest = manifest_commands.register_remote_manifest(
                    session,
                    job_id,
                    request(worker),
                )
            except ValueError as exc:
                return "value_error", str(exc)
            return "success", manifest.manifest_path

    try:
        with session_factory() as session:
            with session.begin():
                session.add(
                    Server(
                        id=server_id,
                        name="Manifest registration test",
                        host="localhost",
                    )
                )
                session.add(
                    Job(
                        id=job_id,
                        input_dir="/shared/input",
                        output_dir="/shared/output",
                        engine="dotsocr",
                        input_mode="remote_folder_snapshot",
                        assigned_server_id=server_id,
                        status="queued",
                    )
                )

        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(register, "worker-a")
            second_future = None
            try:
                assert first_lock_acquired.wait(timeout=10), (
                    "first registration did not reach has_static_shards "
                    "after acquiring the Job row lock"
                )
                second_future = executor.submit(
                    register,
                    "worker-b",
                    observe_backend=True,
                )
                assert second_backend_ready.wait(timeout=5), (
                    "second registration did not expose its PostgreSQL backend"
                )

                lock_observed = False
                last_wait_state = None
                deadline = time.monotonic() + 4
                with engine.connect() as observer:
                    while time.monotonic() < deadline:
                        row = observer.execute(
                            text(
                                """
                                SELECT wait_event_type, wait_event, state
                                FROM pg_stat_activity
                                WHERE pid = :pid
                                """
                            ),
                            {"pid": second_backend_pid[0]},
                        ).mappings().one_or_none()
                        if row is not None:
                            last_wait_state = (
                                row["wait_event_type"],
                                row["wait_event"],
                                row["state"],
                            )
                            if row["wait_event_type"] == "Lock":
                                lock_observed = True
                                break
                        if second_future.done():
                            break
                        time.sleep(0.05)

                assert lock_observed, (
                    "second registration was not observed waiting on the "
                    f"Job row lock; last state={last_wait_state!r}"
                )
                release_first.set()
                outcomes = [
                    first_future.result(timeout=20),
                    second_future.result(timeout=20),
                ]
            finally:
                release_first.set()

        assert sorted(kind for kind, detail in outcomes) == [
            "success",
            "value_error",
        ]
        error = next(
            detail for kind, detail in outcomes if kind == "value_error"
        )
        assert error == f"{expected_error}: {job_id}"

        with session_factory() as session:
            assert session.scalar(
                select(func.count(Manifest.id)).where(
                    Manifest.job_id == job_id
                )
            ) == 1
            shards = list(
                session.scalars(
                    select(WorkShard)
                    .where(WorkShard.job_id == job_id)
                    .order_by(WorkShard.shard_index)
                )
            )
            assert [shard.shard_index for shard in shards] == list(
                range(1, shard_count + 1)
            )
            assert all(
                shard.shard_path.startswith(
                    next(
                        detail
                        for kind, detail in outcomes
                        if kind == "success"
                    ).rsplit("/", 1)[0]
                )
                for shard in shards
            )
    finally:
        with session_factory() as session:
            with session.begin():
                job = session.get(Job, job_id)
                if job is not None:
                    session.delete(job)
                    session.flush()
                server = session.get(Server, server_id)
                if server is not None:
                    session.delete(server)
        engine.dispose()
