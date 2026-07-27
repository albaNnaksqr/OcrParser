from __future__ import annotations

from sqlalchemy.orm import Session as _Session

from ...models import Manifest as _Manifest
from ...schemas import (
    RemoteManifestRegisterRequest as _RemoteManifestRegisterRequest,
)
from ..common import ScanUnitAttemptConflictError, ShardAttemptConflictError
from . import core as _core
from .core import (
    claim_next_pending_shard,
    claim_next_scan_unit,
    claim_worker_manifest_integrity_check,
    complete_scan_unit,
    complete_worker_manifest_integrity_check,
    fail_scan_unit,
    request_worker_manifest_integrity_check,
    update_work_shard,
)


class ManifestCommandTransactionError(RuntimeError):
    """Raised when a Manifest command cannot own its transaction."""


REGISTER_REMOTE_MANIFEST_ACTIVE_TRANSACTION_ERROR = (
    "register_remote_manifest requires a session without an active transaction"
)


def register_remote_manifest(
    session: _Session,
    job_id: str,
    request: _RemoteManifestRegisterRequest,
) -> _Manifest:
    if session.in_transaction():
        raise ManifestCommandTransactionError(
            REGISTER_REMOTE_MANIFEST_ACTIVE_TRANSACTION_ERROR
        )

    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            manifest = _core.register_remote_manifest(
                session,
                job_id,
                request,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return manifest


__all__ = [name for name in globals() if not name.startswith("_")]
