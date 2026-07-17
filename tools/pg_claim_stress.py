#!/usr/bin/env python3
"""PostgreSQL claim/reclaim stress check for the OCR control plane.

Run this against a disposable production-like PostgreSQL database before large
gray tests. The script seeds an isolated job, concurrently claims shards through
the real service layer, and verifies that every shard or scan unit is claimed
once.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import sys
import uuid
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Sequence

from sqlalchemy.orm import Session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.models import Job, Manifest, ScanUnit, Server, WorkShard
from ocr_platform.control.service import (
    POOL_SERVER_ID,
    ShardAttemptConflictError,
    claim_next_pending_shard,
    claim_next_scan_unit,
    complete_scan_unit,
    update_work_shard,
)
from ocr_platform.control.schemas import ScanUnitCompleteRequest, WorkShardUpdateRequest


@dataclass(frozen=True)
class ClaimAnalysis:
    requested_shards: int
    claimed_shards: int
    unique_claims: int
    duplicate_claims: dict[int, int]
    missing_claims: int
    ok: bool


@dataclass(frozen=True)
class ScanUnitClaimAnalysis:
    requested_scan_units: int
    claimed_scan_units: int
    unique_claims: int
    duplicate_claims: dict[int, int]
    missing_claims: int
    ok: bool


@dataclass(frozen=True)
class ScanUnitShardAnalysis:
    expected_shards: int
    generated_shards: int
    unique_shard_indexes: int
    duplicate_shard_indexes: dict[int, int]
    missing_shard_indexes: list[int]
    ok: bool


def planned_concurrency_checks(*, scan_unit_count: int, scan_unit_shards: int) -> list[str]:
    checks = [
        "shard_claim_skip_locked",
        "stale_attempt_rejection",
    ]
    if scan_unit_count:
        checks.append("scan_unit_claim_skip_locked")
        if scan_unit_shards:
            checks.append("scan_unit_completion_shard_index_locking")
    return checks


def require_postgresql_url(database_url: str) -> str:
    if not database_url.startswith(("postgresql://", "postgresql+psycopg://", "postgresql+psycopg2://")):
        raise ValueError("PostgreSQL database URL is required for this stress check.")
    return database_url


def analyze_claimed_shards(*, requested_shards: int, claimed_shard_ids: Sequence[int]) -> ClaimAnalysis:
    counts = Counter(claimed_shard_ids)
    duplicate_claims = {shard_id: count for shard_id, count in sorted(counts.items()) if count > 1}
    unique_claims = len(counts)
    missing_claims = max(requested_shards - unique_claims, 0)
    return ClaimAnalysis(
        requested_shards=requested_shards,
        claimed_shards=len(claimed_shard_ids),
        unique_claims=unique_claims,
        duplicate_claims=duplicate_claims,
        missing_claims=missing_claims,
        ok=(not duplicate_claims and missing_claims == 0),
    )


def analyze_claimed_scan_units(
    *,
    requested_scan_units: int,
    claimed_scan_unit_ids: Sequence[int],
) -> ScanUnitClaimAnalysis:
    counts = Counter(claimed_scan_unit_ids)
    duplicate_claims = {unit_id: count for unit_id, count in sorted(counts.items()) if count > 1}
    unique_claims = len(counts)
    missing_claims = max(requested_scan_units - unique_claims, 0)
    return ScanUnitClaimAnalysis(
        requested_scan_units=requested_scan_units,
        claimed_scan_units=len(claimed_scan_unit_ids),
        unique_claims=unique_claims,
        duplicate_claims=duplicate_claims,
        missing_claims=missing_claims,
        ok=(not duplicate_claims and missing_claims == 0),
    )


def analyze_completed_scan_unit_shards(
    *,
    expected_shards: int,
    shard_indexes: Sequence[int],
) -> ScanUnitShardAnalysis:
    counts = Counter(shard_indexes)
    duplicate_shard_indexes = {index: count for index, count in sorted(counts.items()) if count > 1}
    expected_index_set = set(range(1, max(expected_shards, 0) + 1))
    missing_shard_indexes = sorted(expected_index_set - set(counts))
    return ScanUnitShardAnalysis(
        expected_shards=expected_shards,
        generated_shards=len(shard_indexes),
        unique_shard_indexes=len(counts),
        duplicate_shard_indexes=duplicate_shard_indexes,
        missing_shard_indexes=missing_shard_indexes,
        ok=(len(shard_indexes) == expected_shards and not duplicate_shard_indexes and not missing_shard_indexes),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stress-check PostgreSQL shard claiming with the real OCR control service layer."
    )
    database = parser.add_mutually_exclusive_group(required=True)
    database.add_argument("--database-url", help="PostgreSQL SQLAlchemy URL for a disposable test DB")
    database.add_argument(
        "--database-url-env-var",
        help="Read the disposable PostgreSQL URL from this environment variable.",
    )
    parser.add_argument("--shards", type=int, default=200, help="Number of pending shards to seed")
    parser.add_argument("--scan-units", type=int, default=0, help="Number of pending distributed scan units to seed")
    parser.add_argument(
        "--scan-unit-shards",
        type=int,
        default=0,
        help=(
            "When --scan-units is set, complete each claimed scan unit with this many "
            "shards and verify global shard_index allocation."
        ),
    )
    parser.add_argument("--workers", type=int, default=16, help="Concurrent claiming workers")
    parser.add_argument("--apply-init-db", action="store_true", help="Run SQLAlchemy create_all/compat init before seeding")
    parser.add_argument("--keep-job", action="store_true", help="Keep the seeded stress job rows for manual inspection")
    parser.add_argument("--json", action="store_true", help="Emit JSON only")
    return parser


def _seed_job(session: Session, *, shard_count: int) -> str:
    run_id = uuid.uuid4().hex[:12]
    server = Server(
        id=f"pg-stress-server-{run_id}",
        name=f"PG Stress Server {run_id}",
        host="pg-claim-stress",
        status="online",
    )
    session.add(server)
    job = Job(
        id=str(uuid.uuid4()),
        input_dir=f"/tmp/pg-claim-stress/{run_id}/input",
        output_dir=f"/tmp/pg-claim-stress/{run_id}/output",
        engine="dotsocr",
        input_mode="remote_folder_snapshot",
        assigned_server=server,
        status="running",
    )
    session.add(job)
    session.flush()
    manifest = Manifest(
        job_id=job.id,
        input_mode="remote_folder_snapshot",
        input_root=job.input_dir,
        manifest_path=f"/tmp/pg-claim-stress/{run_id}/manifest.jsonl",
        file_count=shard_count,
        total_bytes=shard_count,
        status="ready",
        next_shard_index=shard_count + 1,
    )
    session.add(manifest)
    session.flush()
    for index in range(1, shard_count + 1):
        session.add(
            WorkShard(
                job_id=job.id,
                manifest_id=manifest.id,
                shard_index=index,
                shard_path=f"/tmp/pg-claim-stress/{run_id}/shard-{index:06d}.jsonl",
                status="pending",
                file_count=1,
            )
        )
    session.commit()
    return job.id


def _seed_scan_unit_job(session: Session, *, scan_unit_count: int) -> str:
    run_id = uuid.uuid4().hex[:12]
    pool_server = session.get(Server, POOL_SERVER_ID)
    if pool_server is None:
        session.add(
            Server(
                id=POOL_SERVER_ID,
                name="Server Pool",
                host="pool",
                status="online",
                capacity_slots=0,
                capabilities_json=json.dumps({"pool": True}),
            )
        )
    job = Job(
        id=str(uuid.uuid4()),
        input_dir=f"/tmp/pg-claim-stress/{run_id}/input",
        output_dir=f"/tmp/pg-claim-stress/{run_id}/output",
        engine="dotsocr",
        input_mode="distributed_remote_folder_snapshot",
        assigned_server_id=POOL_SERVER_ID,
        status="running",
    )
    session.add(job)
    session.flush()
    session.add(
        Manifest(
            job_id=job.id,
            input_mode="distributed_remote_folder_snapshot",
            input_root=job.input_dir,
            manifest_path=f"/tmp/pg-claim-stress/{run_id}/manifest.jsonl",
            file_count=0,
            total_bytes=0,
            status="scanning",
        )
    )
    for index in range(1, scan_unit_count + 1):
        session.add(
            ScanUnit(
                job_id=job.id,
                path=f"{job.input_dir}/dir-{index:06d}",
                status="pending",
            )
        )
    session.commit()
    return job.id


def _claim_worker(session_factory: sessionmaker[Session], *, job_id: str, server_id: str) -> list[int]:
    claimed: list[int] = []
    while True:
        with session_factory() as session:
            shard = claim_next_pending_shard(session, job_id, server_id)
            if shard is None:
                return claimed
            claimed.append(shard.id)
            update_work_shard(
                session,
                shard.id,
                WorkShardUpdateRequest(
                    status="succeeded",
                    assigned_server_id=server_id,
                    attempt_count=shard.attempt_count,
                    processed_files=shard.file_count,
                ),
            )


def _claim_scan_unit_worker(session_factory: sessionmaker[Session], *, server_id: str) -> list[int]:
    claimed: list[int] = []
    while True:
        with session_factory() as session:
            unit = claim_next_scan_unit(session, server_id)
            if unit is None:
                return claimed
            claimed.append(unit.id)


def _complete_scan_unit_worker(
    session_factory: sessionmaker[Session],
    *,
    server_id: str,
    shards_per_scan_unit: int,
) -> list[int]:
    completed: list[int] = []
    while True:
        with session_factory() as session:
            unit = claim_next_scan_unit(session, server_id)
            if unit is None:
                return completed
            completed.append(unit.id)
            complete_scan_unit(
                session,
                unit.id,
                ScanUnitCompleteRequest(
                    assigned_server_id=server_id,
                    attempt_count=unit.attempt_count,
                    manifest_path=f"{unit.path}/manifest.jsonl",
                    meta_path=f"{unit.path}/manifest.meta.json",
                    file_count=shards_per_scan_unit,
                    total_bytes=shards_per_scan_unit,
                    child_paths=[],
                    shards=[
                        {
                            "shard_index": index,
                            "shard_path": f"{unit.path}/shards/shard-{index:06d}.jsonl",
                            "file_count": 1,
                        }
                        for index in range(1, shards_per_scan_unit + 1)
                    ],
                ),
            )


def _verify_attempt_conflict(session_factory: sessionmaker[Session], *, job_id: str) -> bool:
    with session_factory() as session:
        shard = session.query(WorkShard).filter_by(job_id=job_id).order_by(WorkShard.id.asc()).first()
        if shard is None:
            return False
        try:
            update_work_shard(
                session,
                shard.id,
                WorkShardUpdateRequest(
                    status="running",
                    assigned_server_id="stale-worker",
                    attempt_count=max(shard.attempt_count - 1, 0),
                    processed_files=0,
                ),
            )
        except ShardAttemptConflictError:
            session.rollback()
            return True
        return False


def run_stress(
    *,
    database_url: str,
    shard_count: int,
    worker_count: int,
    scan_unit_count: int = 0,
    scan_unit_shards: int = 0,
    apply_init_db: bool = False,
    keep_job: bool = False,
) -> dict[str, object]:
    require_postgresql_url(database_url)
    if shard_count <= 0:
        raise ValueError("--shards must be positive")
    if scan_unit_count < 0:
        raise ValueError("--scan-units must be non-negative")
    if scan_unit_shards < 0:
        raise ValueError("--scan-unit-shards must be non-negative")
    if worker_count <= 0:
        raise ValueError("--workers must be positive")

    session_factory, engine = create_session_factory(database_url)
    if apply_init_db:
        init_db(engine)
    with session_factory() as session:
        job_id = _seed_job(session, shard_count=shard_count)
        scan_unit_job_id = (
            _seed_scan_unit_job(session, scan_unit_count=scan_unit_count)
            if scan_unit_count
            else None
        )

    worker_run_id = uuid.uuid4().hex[:12]
    server_ids = [f"pg-stress-worker-{worker_run_id}-{index}" for index in range(worker_count)]
    with session_factory() as session:
        for server_id in server_ids:
            session.add(
                Server(
                    id=server_id,
                    name=server_id,
                    host=server_id,
                    status="online",
                    capabilities_json=json.dumps(
                        {
                            "shared_paths": [
                                {
                                    "path": "/tmp/pg-claim-stress",
                                    "exists": True,
                                    "is_dir": True,
                                    "readable": True,
                                    "writable": True,
                                }
                            ]
                        }
                    ),
                )
            )
        session.commit()

    claimed_ids: list[int] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [
            executor.submit(_claim_worker, session_factory, job_id=job_id, server_id=server_id)
            for server_id in server_ids
        ]
        for future in concurrent.futures.as_completed(futures):
            claimed_ids.extend(future.result())

    analysis = analyze_claimed_shards(requested_shards=shard_count, claimed_shard_ids=claimed_ids)
    attempt_conflict_rejected = _verify_attempt_conflict(session_factory, job_id=job_id)
    result = {
        **asdict(analysis),
        "job_id": job_id,
        "worker_count": worker_count,
        "checks": planned_concurrency_checks(
            scan_unit_count=scan_unit_count,
            scan_unit_shards=scan_unit_shards,
        ),
        "attempt_conflict_rejected": attempt_conflict_rejected,
        "ok": analysis.ok and attempt_conflict_rejected,
    }
    if scan_unit_count:
        claimed_scan_unit_ids: list[int] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=worker_count) as executor:
            if scan_unit_shards:
                futures = [
                    executor.submit(
                        _complete_scan_unit_worker,
                        session_factory,
                        server_id=server_id,
                        shards_per_scan_unit=scan_unit_shards,
                    )
                    for server_id in server_ids
                ]
            else:
                futures = [
                    executor.submit(_claim_scan_unit_worker, session_factory, server_id=server_id)
                    for server_id in server_ids
                ]
            for future in concurrent.futures.as_completed(futures):
                claimed_scan_unit_ids.extend(future.result())
        scan_unit_analysis = analyze_claimed_scan_units(
            requested_scan_units=scan_unit_count,
            claimed_scan_unit_ids=claimed_scan_unit_ids,
        )
        result["scan_unit_job_id"] = scan_unit_job_id
        result["scan_unit_claims"] = asdict(scan_unit_analysis)
        result["ok"] = bool(result["ok"]) and scan_unit_analysis.ok
        if scan_unit_shards:
            expected_shards = scan_unit_count * scan_unit_shards
            with session_factory() as session:
                shard_indexes = [
                    int(index)
                    for (index,) in session.query(WorkShard.shard_index)
                    .filter_by(job_id=scan_unit_job_id)
                    .order_by(WorkShard.shard_index.asc())
                    .all()
                ]
            scan_unit_shard_analysis = analyze_completed_scan_unit_shards(
                expected_shards=expected_shards,
                shard_indexes=shard_indexes,
            )
            result["scan_unit_completion_shards"] = asdict(scan_unit_shard_analysis)
            result["ok"] = bool(result["ok"]) and scan_unit_shard_analysis.ok

    if not keep_job:
        with session_factory() as session:
            job = session.get(Job, job_id)
            if job is not None:
                session.delete(job)
            if scan_unit_job_id is not None:
                scan_unit_job = session.get(Job, scan_unit_job_id)
                if scan_unit_job is not None:
                    session.delete(scan_unit_job)
            session.query(Server).filter(Server.id.in_(server_ids)).delete(
                synchronize_session=False
            )
            session.commit()
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    database_url = args.database_url
    if args.database_url_env_var:
        database_url = os.getenv(args.database_url_env_var, "")
        if not database_url:
            parser.error(
                f"environment variable {args.database_url_env_var} is required and must be non-empty"
            )
    try:
        result = run_stress(
            database_url=database_url,
            shard_count=args.shards,
            scan_unit_count=args.scan_units,
            scan_unit_shards=args.scan_unit_shards,
            worker_count=args.workers,
            apply_init_db=args.apply_init_db,
            keep_job=args.keep_job,
        )
    except Exception as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        else:
            print(f"pg claim stress failed: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print("PostgreSQL claim stress result:")
        for key, value in result.items():
            print(f"  {key}: {value}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
