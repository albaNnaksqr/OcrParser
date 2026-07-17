import importlib.util
import sys
from pathlib import Path

import pytest


TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / "pg_claim_stress.py"


def load_tool():
    spec = importlib.util.spec_from_file_location("pg_claim_stress", TOOL_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_pg_claim_stress_requires_postgresql_url():
    tool = load_tool()

    assert tool.require_postgresql_url("postgresql+psycopg://u:p@db/ocr") == "postgresql+psycopg://u:p@db/ocr"

    with pytest.raises(ValueError, match="PostgreSQL"):
        tool.require_postgresql_url("sqlite:///local.db")


def test_pg_claim_stress_detects_duplicate_or_missing_claims():
    tool = load_tool()

    healthy = tool.analyze_claimed_shards(
        requested_shards=3,
        claimed_shard_ids=[1, 2, 3],
    )
    assert healthy.ok is True
    assert healthy.duplicate_claims == {}
    assert healthy.missing_claims == 0

    broken = tool.analyze_claimed_shards(
        requested_shards=4,
        claimed_shard_ids=[1, 1, 2],
    )
    assert broken.ok is False
    assert broken.duplicate_claims == {1: 2}
    assert broken.missing_claims == 2


def test_pg_claim_stress_detects_duplicate_or_missing_scan_unit_claims():
    tool = load_tool()

    healthy = tool.analyze_claimed_scan_units(
        requested_scan_units=2,
        claimed_scan_unit_ids=[10, 11],
    )
    assert healthy.ok is True
    assert healthy.duplicate_claims == {}
    assert healthy.missing_claims == 0

    broken = tool.analyze_claimed_scan_units(
        requested_scan_units=3,
        claimed_scan_unit_ids=[10, 10],
    )
    assert broken.ok is False
    assert broken.duplicate_claims == {10: 2}
    assert broken.missing_claims == 2


def test_pg_claim_stress_detects_duplicate_or_missing_completed_scan_unit_shard_indexes():
    tool = load_tool()

    healthy = tool.analyze_completed_scan_unit_shards(
        expected_shards=4,
        shard_indexes=[1, 2, 3, 4],
    )
    assert healthy.ok is True
    assert healthy.duplicate_shard_indexes == {}
    assert healthy.missing_shard_indexes == []

    broken = tool.analyze_completed_scan_unit_shards(
        expected_shards=5,
        shard_indexes=[1, 1, 2, 4],
    )
    assert broken.ok is False
    assert broken.duplicate_shard_indexes == {1: 2}
    assert broken.missing_shard_indexes == [3, 5]


def test_pg_claim_stress_parser_exposes_production_knobs():
    tool = load_tool()
    parser = tool.build_parser()

    args = parser.parse_args(
        [
            "--database-url",
            "postgresql+psycopg://u:p@db/ocr",
            "--shards",
            "25",
            "--workers",
            "5",
            "--scan-units",
            "7",
            "--scan-unit-shards",
            "2",
        ]
    )

    assert args.database_url.startswith("postgresql")
    assert args.shards == 25
    assert args.workers == 5
    assert args.scan_units == 7
    assert args.scan_unit_shards == 2

    env_args = parser.parse_args(["--database-url-env-var", "OCR_SOAK_DATABASE_URL"])
    assert env_args.database_url is None
    assert env_args.database_url_env_var == "OCR_SOAK_DATABASE_URL"


def test_pg_claim_stress_describes_planned_concurrency_checks():
    tool = load_tool()

    shard_only = tool.planned_concurrency_checks(scan_unit_count=0, scan_unit_shards=0)
    assert shard_only == [
        "shard_claim_skip_locked",
        "stale_attempt_rejection",
    ]

    with_scan_units = tool.planned_concurrency_checks(scan_unit_count=3, scan_unit_shards=0)
    assert with_scan_units == [
        "shard_claim_skip_locked",
        "stale_attempt_rejection",
        "scan_unit_claim_skip_locked",
    ]

    with_completion = tool.planned_concurrency_checks(scan_unit_count=3, scan_unit_shards=2)
    assert with_completion == [
        "shard_claim_skip_locked",
        "stale_attempt_rejection",
        "scan_unit_claim_skip_locked",
        "scan_unit_completion_shard_index_locking",
    ]


def test_pg_claim_stress_uses_run_scoped_worker_ids():
    source = TOOL_PATH.read_text(encoding="utf-8")

    assert 'worker_run_id = uuid.uuid4().hex[:12]' in source
    assert 'f"pg-stress-worker-{worker_run_id}-{index}"' in source
    assert "session.query(Server).filter(Server.id.in_(server_ids)).delete" in source
    assert "if scan_unit_job_id is not None:\n                scan_unit_job =" in source
