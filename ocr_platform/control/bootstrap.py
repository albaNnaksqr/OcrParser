from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Sequence

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from . import database
from .domains.common import DEFAULT_MODEL_PROFILES, json_dumps
from .domains.remote_admin.ports import RemoteWorkerPort
from .models import ModelProfile
from .remote_workers import RemoteWorkerExecutor
from .settings import ControlSettings


BOOTSTRAP_FAILED_ERROR = "Control database bootstrap failed."


@dataclass(frozen=True, slots=True)
class BootstrapResult:
    status: str
    inserted_profiles: int = 0


@dataclass(frozen=True, slots=True)
class ControlRuntime:
    settings: ControlSettings
    session_factory: sessionmaker[Session]
    engine: Engine
    remote_worker_executor: RemoteWorkerPort
    owns_engine: bool


def build_control_runtime(
    *,
    settings: ControlSettings | None = None,
    session_factory: sessionmaker[Session] | None = None,
    remote_worker_executor: RemoteWorkerPort | None = None,
) -> ControlRuntime:
    control_settings = (
        settings if settings is not None else ControlSettings.from_environment()
    )
    executor = (
        remote_worker_executor
        if remote_worker_executor is not None
        else RemoteWorkerExecutor()
    )
    owns_engine = session_factory is None
    if session_factory is None:
        session_factory, db_engine = database.create_session_factory(
            settings=control_settings
        )
    else:
        with session_factory() as session:
            db_engine = session.get_bind()
    return ControlRuntime(
        settings=control_settings,
        session_factory=session_factory,
        engine=db_engine,
        remote_worker_executor=executor,
        owns_engine=owns_engine,
    )


def _default_profile_rows() -> list[dict[str, object]]:
    return [
        {
            "id": profile_id,
            "label": str(defaults["label"]),
            "engine": str(defaults["engine"]),
            "ip": defaults.get("ip"),
            "port": defaults.get("port"),
            "model_name": defaults.get("model_name"),
            "page_concurrency": defaults.get("page_concurrency"),
            "extra_args_json": json_dumps(defaults.get("extra_args", {})),
            "requires_api_key": bool(
                defaults.get("requires_api_key", False)
            ),
            "is_default": bool(defaults.get("is_default", False)),
        }
        for profile_id, defaults in DEFAULT_MODEL_PROFILES.items()
    ]


def seed_default_model_profiles(session: Session) -> int:
    rows = _default_profile_rows()
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(ModelProfile).values(rows)
        statement = statement.on_conflict_do_nothing(
            index_elements=[ModelProfile.id]
        ).returning(ModelProfile.id)
        uses_returning = True
    elif dialect == "sqlite":
        statement = sqlite_insert(ModelProfile).values(rows)
        statement = statement.on_conflict_do_nothing(
            index_elements=[ModelProfile.id]
        )
        uses_returning = False
    else:
        raise RuntimeError(
            "Default model-profile bootstrap supports SQLite and PostgreSQL."
        )
    result = session.execute(statement)
    if uses_returning:
        return len(result.scalars().all())
    return max(int(result.rowcount or 0), 0)


def bootstrap_control_database(
    session_factory: sessionmaker[Session],
    db_engine,
) -> BootstrapResult:
    dialect = db_engine.dialect.name
    try:
        status = database.describe_database_status(db_engine)
    except Exception:
        if dialect == "postgresql":
            return BootstrapResult("database_unavailable")
        raise
    if dialect == "postgresql" and not status.get("is_current"):
        return BootstrapResult("schema_not_current")
    try:
        with session_factory() as session:
            with session.begin():
                inserted = seed_default_model_profiles(session)
    except Exception:
        raise RuntimeError(BOOTSTRAP_FAILED_ERROR) from None
    return BootstrapResult("complete", inserted_profiles=inserted)


def main(argv: Sequence[str] | None = None) -> int:
    del argv
    settings = ControlSettings.from_environment()
    session_factory, db_engine = database.create_session_factory(
        settings=settings
    )
    try:
        result = bootstrap_control_database(session_factory, db_engine)
    finally:
        db_engine.dispose()
    print(
        json.dumps(
            {
                "status": result.status,
                "inserted_profiles": result.inserted_profiles,
            },
            sort_keys=True,
        )
    )
    return 0 if result.status == "complete" else 2


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BootstrapResult",
    "BOOTSTRAP_FAILED_ERROR",
    "ControlRuntime",
    "bootstrap_control_database",
    "build_control_runtime",
    "main",
    "seed_default_model_profiles",
]
