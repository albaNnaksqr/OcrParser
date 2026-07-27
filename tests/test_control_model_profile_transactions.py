from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.orm import sessionmaker

from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.domains.model_profiles import commands, core, queries
from ocr_platform.control.domains.model_profiles.commands import (
    ACTIVE_TRANSACTION_ERROR,
    ModelProfileTransactionError,
)
from ocr_platform.control.models import (
    ModelProfile,
    ModelProfileCertification,
)
from ocr_platform.control.schemas import ModelProfileRequest
from ocr_platform.control.settings import ControlSettings


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64


def _database(tmp_path):
    session_factory, engine = create_session_factory(
        f"sqlite:///{tmp_path / 'control.db'}"
    )
    init_db(engine)
    return session_factory, engine


def _request(**overrides) -> ModelProfileRequest:
    values = {
        "label": "Model profile",
        "engine": "dotsocr",
        "extra_args": {},
    }
    values.update(overrides)
    return ModelProfileRequest(**values)


def _certified() -> dict[str, str]:
    return {
        "enforcement": "certified",
        "status": "certified",
        "parser_revision": "parser-r2",
        "model_revision": "model-r2",
        "runtime_digest": SHA_A,
        "fixture_set_digest": SHA_B,
        "evidence_digest": SHA_C,
    }


def test_upsert_command_commits_exactly_once(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    commits: list[int] = []
    rollbacks: list[int] = []

    try:
        with session_factory() as session:
            event.listen(
                session,
                "after_commit",
                lambda current: commits.append(1),
            )
            event.listen(
                session,
                "after_rollback",
                lambda current: rollbacks.append(1),
            )

            profile = commands.upsert_model_profile(
                session,
                "profile-a",
                _request(label="Committed profile"),
            )

            assert profile.label == "Committed profile"
            assert profile.created_at is not None
            assert profile.updated_at is not None
            assert commits == [1]
            assert rollbacks == []

        with session_factory() as session:
            assert session.get(ModelProfile, "profile-a").label == (
                "Committed profile"
            )
    finally:
        engine.dispose()


def test_upsert_result_remains_readable_with_expiring_sessionmaker(
    tmp_path,
) -> None:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'expiring-control.db'}",
        future=True,
    )
    standard_session_factory = sessionmaker(bind=engine)
    init_db(engine)

    try:
        with standard_session_factory() as session:
            assert session.expire_on_commit is True
            created = commands.upsert_model_profile(
                session,
                "profile-a",
                _request(label="Created profile"),
            )
            assert session.expire_on_commit is True
            assert session.in_transaction() is False
            assert inspect(created).persistent is True
            created_values = (
                created.id,
                created.label,
                created.created_at,
                created.updated_at,
            )
            assert session.in_transaction() is False

        assert inspect(created).detached is True
        assert (
            created.id,
            created.label,
            created.created_at,
            created.updated_at,
        ) == created_values

        with standard_session_factory() as session:
            updated = commands.upsert_model_profile(
                session,
                "profile-a",
                _request(label="Updated profile"),
            )
            assert session.expire_on_commit is True
            assert session.in_transaction() is False
            assert inspect(updated).persistent is True
            updated_values = (
                updated.id,
                updated.label,
                updated.created_at,
                updated.updated_at,
            )
            assert session.in_transaction() is False

        assert inspect(updated).detached is True
        assert (
            updated.id,
            updated.label,
            updated.created_at,
            updated.updated_at,
        ) == updated_values
        assert updated.label == "Updated profile"
    finally:
        engine.dispose()


@pytest.mark.parametrize("transaction_mode", ["explicit", "autobegin"])
def test_command_rejects_active_transaction_without_owning_it(
    tmp_path,
    transaction_mode,
) -> None:
    session_factory, engine = _database(tmp_path)
    commits: list[int] = []
    rollbacks: list[int] = []
    outer_profile_id = f"outer-{transaction_mode}"

    try:
        with session_factory() as session:
            event.listen(
                session,
                "after_commit",
                lambda current: commits.append(1),
            )
            event.listen(
                session,
                "after_rollback",
                lambda current: rollbacks.append(1),
            )
            if transaction_mode == "explicit":
                session.begin()
            else:
                assert session.get(ModelProfile, "missing") is None
            outer_profile = ModelProfile(
                id=outer_profile_id,
                label="Outer transaction",
                engine="dotsocr",
            )
            session.add(outer_profile)

            with pytest.raises(
                ModelProfileTransactionError,
                match=f"^{ACTIVE_TRANSACTION_ERROR}$",
            ):
                commands.upsert_model_profile(
                    session,
                    "profile-a",
                    _request(),
                )

            assert session.in_transaction() is True
            assert outer_profile in session.new
            assert commits == []
            assert rollbacks == []
            session.rollback()
            assert commits == []
            assert rollbacks == [1]

        with session_factory() as session:
            assert session.get(ModelProfile, outer_profile_id) is None
            assert session.get(ModelProfile, "profile-a") is None
    finally:
        engine.dispose()


def test_upsert_leaf_failure_rolls_back_once(tmp_path, monkeypatch) -> None:
    session_factory, engine = _database(tmp_path)
    commits: list[int] = []
    rollbacks: list[int] = []

    def fail_leaf(*args, **kwargs):
        raise RuntimeError("leaf failed")

    monkeypatch.setattr(core, "upsert_model_profile", fail_leaf)
    try:
        with session_factory() as session:
            event.listen(
                session,
                "after_commit",
                lambda current: commits.append(1),
            )
            event.listen(
                session,
                "after_rollback",
                lambda current: rollbacks.append(1),
            )

            with pytest.raises(RuntimeError, match="leaf failed"):
                commands.upsert_model_profile(
                    session,
                    "profile-a",
                    _request(),
                )

            assert commits == []
            assert rollbacks == [1]
            assert session.in_transaction() is False

        with session_factory() as session:
            assert session.get(ModelProfile, "profile-a") is None
    finally:
        engine.dispose()


def test_default_key_and_certification_roll_back_atomically(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    commits: list[int] = []
    rollbacks: list[int] = []

    try:
        with session_factory() as session:
            with session.begin():
                session.add_all(
                    [
                        ModelProfile(
                            id="profile-default",
                            label="Existing default",
                            engine="dotsocr",
                            is_default=True,
                        ),
                        ModelProfile(
                            id="profile-target",
                            label="Original target",
                            engine="dotsocr",
                            api_key="original-secret",
                            is_default=False,
                            certification=ModelProfileCertification(
                                status="contract_only",
                                enforcement="off",
                            ),
                        ),
                    ]
                )

        with session_factory() as session:
            event.listen(
                session,
                "after_commit",
                lambda current: commits.append(1),
            )
            event.listen(
                session,
                "after_rollback",
                lambda current: rollbacks.append(1),
            )

            def fail_flush(current, flush_context, instances):
                raise RuntimeError("flush failed")

            event.listen(session, "before_flush", fail_flush)
            with pytest.raises(RuntimeError, match="flush failed"):
                commands.upsert_model_profile(
                    session,
                    "profile-target",
                    _request(
                        label="Changed target",
                        engine="mineru",
                        api_key="replacement-secret",
                        is_default=True,
                        certification=_certified(),
                    ),
                    settings=ControlSettings(
                        saved_model_profile_keys_allowed=True
                    ),
                )

            assert commits == []
            assert rollbacks == [1]

        with session_factory() as session:
            default = session.get(ModelProfile, "profile-default")
            target = session.get(ModelProfile, "profile-target")
            certification = session.get(
                ModelProfileCertification,
                "profile-target",
            )
            assert default.is_default is True
            assert target.is_default is False
            assert target.label == "Original target"
            assert target.engine == "dotsocr"
            assert target.api_key == "original-secret"
            assert certification.status == "contract_only"
            assert certification.enforcement == "off"
            assert certification.parser_revision is None
            assert certification.runtime_digest is None
    finally:
        engine.dispose()


def test_list_query_is_select_only(tmp_path) -> None:
    session_factory, engine = _database(tmp_path)
    statements: list[str] = []
    flushes: list[int] = []

    try:
        with session_factory() as session:
            commands.upsert_model_profile(
                session,
                "profile-a",
                _request(),
            )

        def record_statement(
            connection,
            cursor,
            statement,
            parameters,
            context,
            executemany,
        ):
            statements.append(statement.lstrip().split(None, 1)[0].upper())

        event.listen(engine, "before_cursor_execute", record_statement)
        try:
            with session_factory() as session:
                event.listen(
                    session,
                    "before_flush",
                    lambda current, context, instances: flushes.append(1),
                )
                profiles = queries.list_model_profiles(session)

                assert [profile.id for profile in profiles] == ["profile-a"]
                assert list(session.new) == []
                assert list(session.dirty) == []
                assert list(session.deleted) == []
        finally:
            event.remove(engine, "before_cursor_execute", record_statement)

        assert statements == ["SELECT"]
        assert flushes == []
    finally:
        engine.dispose()
