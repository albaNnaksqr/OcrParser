from __future__ import annotations

from sqlalchemy import select as _select
from sqlalchemy.orm import Session as _Session

from ... import scheduling as _scheduling
from ...limits import ControlLimits as _ControlLimits
from ...models import Manifest as _Manifest
from ...models import ScanUnit as _ScanUnit
from ...models import WorkShard as _WorkShard
from ...schemas import (
    ManifestIntegrityWorkerCompleteRequest as _ManifestIntegrityWorkerCompleteRequest,
    RemoteManifestRegisterRequest as _RemoteManifestRegisterRequest,
    ScanUnitCompleteRequest as _ScanUnitCompleteRequest,
    ScanUnitFailRequest as _ScanUnitFailRequest,
    WorkShardUpdateRequest as _WorkShardUpdateRequest,
)
from ..common import ScanUnitAttemptConflictError, ShardAttemptConflictError
from . import construction as _construction
from . import integrity as _integrity
from . import use_cases as _use_cases


class ManifestCommandTransactionError(RuntimeError):
    """Raised when a Manifest command cannot own its transaction."""


REGISTER_REMOTE_MANIFEST_ACTIVE_TRANSACTION_ERROR = (
    "register_remote_manifest requires a session without an active transaction"
)
COMPLETE_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR = (
    "complete_scan_unit requires a session without an active transaction"
)
FAIL_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR = (
    "fail_scan_unit requires a session without an active transaction"
)
CLAIM_NEXT_PENDING_SHARD_ACTIVE_TRANSACTION_ERROR = (
    "claim_next_pending_shard requires a session without an active transaction"
)
CLAIM_NEXT_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR = (
    "claim_next_scan_unit requires a session without an active transaction"
)
UPDATE_WORK_SHARD_ACTIVE_TRANSACTION_ERROR = (
    "update_work_shard requires a session without an active transaction"
)
REQUEST_WORKER_INTEGRITY_ACTIVE_TRANSACTION_ERROR = (
    "request_worker_manifest_integrity_check requires a session without an active transaction"
)
CLAIM_WORKER_INTEGRITY_ACTIVE_TRANSACTION_ERROR = (
    "claim_worker_manifest_integrity_check requires a session without an active transaction"
)
COMPLETE_WORKER_INTEGRITY_ACTIVE_TRANSACTION_ERROR = (
    "complete_worker_manifest_integrity_check requires a session without an active transaction"
)


class _ScanUnitClaimPhaseEnded(RuntimeError):
    def __init__(self, now, *, server_available: bool = True) -> None:
        super().__init__("scan unit claim phase ended")
        self.now = now
        self.server_available = server_available


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
            manifest = _construction.register_remote_manifest(
                session,
                job_id,
                request,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return manifest


def complete_scan_unit(
    session: _Session,
    scan_unit_id: int,
    request: _ScanUnitCompleteRequest,
    *,
    limits: _ControlLimits | None = None,
) -> _ScanUnit:
    if session.in_transaction():
        raise ManifestCommandTransactionError(
            COMPLETE_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR
        )

    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            unit = _use_cases.complete_scan_unit(
                session,
                scan_unit_id,
                request,
                limits=limits,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return unit


def fail_scan_unit(
    session: _Session,
    scan_unit_id: int,
    request: _ScanUnitFailRequest,
) -> _ScanUnit:
    if session.in_transaction():
        raise ManifestCommandTransactionError(
            FAIL_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR
        )

    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            unit = _use_cases.fail_scan_unit(
                session,
                scan_unit_id,
                request,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return unit


def request_worker_manifest_integrity_check(
    session: _Session,
    job_id: str,
):
    if session.in_transaction():
        raise ManifestCommandTransactionError(
            REQUEST_WORKER_INTEGRITY_ACTIVE_TRANSACTION_ERROR
        )
    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            response = _integrity.request_worker_manifest_integrity_check(
                session,
                job_id,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return response


def claim_worker_manifest_integrity_check(
    session: _Session,
    server_id: str,
):
    if session.in_transaction():
        raise ManifestCommandTransactionError(
            CLAIM_WORKER_INTEGRITY_ACTIVE_TRANSACTION_ERROR
        )
    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            task = _integrity.claim_worker_manifest_integrity_check(
                session,
                server_id,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return task


def complete_worker_manifest_integrity_check(
    session: _Session,
    manifest_id: int,
    server_id: str,
    request: _ManifestIntegrityWorkerCompleteRequest,
    *,
    limits: _ControlLimits | None = None,
):
    if session.in_transaction():
        raise ManifestCommandTransactionError(
            COMPLETE_WORKER_INTEGRITY_ACTIVE_TRANSACTION_ERROR
        )
    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            response = _integrity.complete_worker_manifest_integrity_check(
                session,
                manifest_id,
                server_id,
                request,
                limits=limits,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return response


def claim_next_pending_shard(
    session: _Session,
    job_id: str,
    server_id: str,
) -> _WorkShard | None:
    if session.in_transaction():
        raise ManifestCommandTransactionError(
            CLAIM_NEXT_PENDING_SHARD_ACTIVE_TRANSACTION_ERROR
        )

    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    collision_parent = None
    try:
        while True:
            try:
                with session.begin():
                    # A lost parent-fenced CAS must leave the previous
                    # transaction before checking whether a retry is valid.
                    if collision_parent is not None:
                        parent_claimable = session.execute(
                            _select(collision_parent)
                        ).scalar_one()
                        if not parent_claimable:
                            return None
                        collision_parent = None
                    shard = _use_cases.claim_next_pending_shard(
                        session,
                        job_id,
                        server_id,
                    )
                return shard
            except _scheduling._WorkShardClaimCollision as exc:
                collision_parent = exc.claimable_parent
    finally:
        session.expire_on_commit = previous_expire_on_commit


def claim_next_scan_unit(
    session: _Session,
    server_id: str,
) -> _ScanUnit | None:
    if session.in_transaction():
        raise ManifestCommandTransactionError(
            CLAIM_NEXT_SCAN_UNIT_ACTIVE_TRANSACTION_ERROR
        )

    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        while True:
            now = None
            restart = False
            for claim_statuses, reconcile in (
                ({"stale"}, True),
                ({"pending"}, False),
            ):
                try:
                    with session.begin():
                        unit, now, server_available = (
                            _use_cases.claim_next_scan_unit_phase(
                                session,
                                server_id,
                                claim_statuses=claim_statuses,
                                now=now,
                                reconcile=reconcile,
                            )
                        )
                        if unit is None:
                            # Phase exhaustion is a rollback boundary: it
                            # releases every skipped candidate lock and, for
                            # the stale phase, rolls back reconciliation before
                            # the pending phase starts.
                            raise _ScanUnitClaimPhaseEnded(
                                now,
                                server_available=server_available,
                            )
                    return unit
                except _ScanUnitClaimPhaseEnded as exc:
                    if not exc.server_available:
                        return None
                    now = exc.now
                except _scheduling._ScanUnitClaimCollision:
                    # Leave the failed transaction before restarting from the
                    # server lookup and stale reconciliation phase.
                    restart = True
                    break
            if not restart:
                return None
    finally:
        session.expire_on_commit = previous_expire_on_commit


def update_work_shard(
    session: _Session,
    shard_id: int,
    request: _WorkShardUpdateRequest,
) -> _WorkShard:
    if session.in_transaction():
        raise ManifestCommandTransactionError(
            UPDATE_WORK_SHARD_ACTIVE_TRANSACTION_ERROR
        )

    previous_expire_on_commit = session.expire_on_commit
    session.expire_on_commit = False
    try:
        with session.begin():
            snapshot = _scheduling.get_work_shard_update_snapshot(
                session,
                shard_id,
            )
            job = None
            if _scheduling.work_shard_update_requires_job_lock(
                requested_status=request.status,
                observed_status=snapshot.status,
            ):
                job = _scheduling._lock_job_for_shard_change(
                    session,
                    snapshot.job_id,
                )
                if job is None:
                    raise ValueError(f"unknown shard: {shard_id}")
            shard = _scheduling.lock_work_shard_for_update(
                session,
                shard_id,
            )
            shard = _scheduling.apply_work_shard_update(
                session,
                shard=shard,
                job=job,
                request=request,
            )
    finally:
        session.expire_on_commit = previous_expire_on_commit
    return shard


__all__ = [name for name in globals() if not name.startswith("_")]
