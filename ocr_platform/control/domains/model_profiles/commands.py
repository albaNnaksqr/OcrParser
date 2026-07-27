from __future__ import annotations

from sqlalchemy.orm import Session

from ...models import ModelProfile
from ...schemas import ModelProfileRequest
from ...settings import ControlSettings
from . import core


class ModelProfileTransactionError(RuntimeError):
    """Raised when a Model Profile command cannot own its transaction."""


ACTIVE_TRANSACTION_ERROR = (
    "upsert_model_profile requires a session without an active transaction"
)


def upsert_model_profile(
    session: Session,
    profile_id: str,
    request: ModelProfileRequest,
    *,
    settings: ControlSettings | None = None,
) -> ModelProfile:
    if session.in_transaction():
        raise ModelProfileTransactionError(ACTIVE_TRANSACTION_ERROR)

    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            profile = core.upsert_model_profile(
                session,
                profile_id,
                request,
                settings=settings,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return profile

__all__ = [
    "ACTIVE_TRANSACTION_ERROR",
    "ModelProfileTransactionError",
    "upsert_model_profile",
]
