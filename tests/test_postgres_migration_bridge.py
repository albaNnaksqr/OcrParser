from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ocr_platform.control.database import create_session_factory
from ocr_platform.control.migration import MigrationCatalog, MigrationRunner
from ocr_platform.control.models import ModelProfile, ModelProfileCertification


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
