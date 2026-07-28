from __future__ import annotations

import ast
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ocr_platform.control import scheduling
from ocr_platform.control.domains.common import ScanUnitAttemptConflictError
from ocr_platform.control.domains.manifests import core, ports
from ocr_platform.control.models import Job, Manifest, ScanUnit
from ocr_platform.control.schemas import (
    RemoteManifestShardRequest,
    ScanUnitCompleteRequest,
    ScanUnitFailRequest,
)


ROOT = Path(__file__).resolve().parents[1]


def _scan_unit(*, status: str = "running") -> ScanUnit:
    return ScanUnit(
        id=7,
        job_id="job-a",
        path="/shared/input",
        status=status,
        assigned_server_id="server-a",
        attempt_count=2,
    )


def test_scan_unit_transition_policy_fences_before_idempotent_replay() -> None:
    completed = _scan_unit(status="succeeded")

    with pytest.raises(
        ScanUnitAttemptConflictError,
        match="different server attempt",
    ):
        scheduling._scan_unit_transition_plan(
            completed,
            assigned_server_id="server-b",
            attempt_count=2,
            terminal_status="succeeded",
            operation="completion",
        )
    with pytest.raises(
        ScanUnitAttemptConflictError,
        match="stale attempt",
    ):
        scheduling._scan_unit_transition_plan(
            completed,
            assigned_server_id="server-a",
            attempt_count=1,
            terminal_status="succeeded",
            operation="completion",
        )

    replay = scheduling._scan_unit_transition_plan(
        completed,
        assigned_server_id="server-a",
        attempt_count=2,
        terminal_status="succeeded",
        operation="completion",
    )

    assert replay.unit is completed
    assert replay.should_apply is False


def test_scan_unit_transition_policy_applies_terminal_fields() -> None:
    completed = _scan_unit()
    completed_at = datetime(2026, 8, 3, 1, 2, 3, tzinfo=timezone.utc)
    completion = scheduling._scan_unit_transition_plan(
        completed,
        assigned_server_id="server-a",
        attempt_count=2,
        terminal_status="succeeded",
        operation="completion",
    )

    returned_completion = scheduling.apply_scan_unit_completion(
        completion,
        manifest_path="/shared/manifest.jsonl",
        meta_path="/shared/manifest.meta.json",
        file_count=3,
        total_bytes=21,
        finished_at=completed_at,
    )

    assert returned_completion is completed
    assert completed.status == "succeeded"
    assert completed.manifest_path == "/shared/manifest.jsonl"
    assert completed.meta_path == "/shared/manifest.meta.json"
    assert completed.file_count == 3
    assert completed.total_bytes == 21
    assert completed.finished_at == completed_at
    assert completed.lease_expires_at is None

    failed = _scan_unit()
    failed_at = datetime(2026, 8, 3, 2, 3, 4, tzinfo=timezone.utc)
    failure = scheduling._scan_unit_transition_plan(
        failed,
        assigned_server_id="server-a",
        attempt_count=2,
        terminal_status="failed",
        operation="failure",
    )

    returned_failure = scheduling.apply_scan_unit_failure(
        failure,
        failure_category="input_invalid",
        error_message="permission denied",
        finished_at=failed_at,
    )

    assert returned_failure is failed
    assert failed.status == "failed"
    assert failed.failure_category == "input_invalid"
    assert failed.error_message == "permission denied"
    assert failed.finished_at == failed_at
    assert failed.lease_expires_at is None


@pytest.mark.parametrize("status", ["pending", "stale", "stopped", "failed"])
def test_scan_unit_transition_policy_rejects_non_running_status(
    status: str,
) -> None:
    unit = _scan_unit(status=status)

    with pytest.raises(
        ScanUnitAttemptConflictError,
        match=f"scan unit is not running: {status}",
    ):
        scheduling._scan_unit_transition_plan(
            unit,
            assigned_server_id="server-a",
            attempt_count=2,
            terminal_status="succeeded",
            operation="completion",
        )


def test_scheduling_factories_own_pending_work_state() -> None:
    unit = scheduling.new_pending_scan_unit(
        job_id="job-a",
        path="/shared/input/child",
    )
    shard = scheduling.new_pending_work_shard(
        job_id="job-a",
        manifest_id=9,
        shard_index=4,
        shard_path="/shared/shard.jsonl",
        file_count=3,
    )

    assert unit.status == "pending"
    assert unit.job_id == "job-a"
    assert unit.path == "/shared/input/child"
    assert shard.status == "pending"
    assert shard.job_id == "job-a"
    assert shard.manifest_id == 9
    assert shard.shard_index == 4


class _SequenceSession:
    def __init__(self, calls: list[str]) -> None:
        self.calls = calls

    def flush(self) -> None:
        self.calls.append("flush")


def test_scan_completion_leaf_preserves_port_and_transition_order(
    monkeypatch,
) -> None:
    calls: list[str] = []
    session = _SequenceSession(calls)
    unit = _scan_unit()
    plan = scheduling.ScanUnitTransitionPlan(
        unit=unit,
        should_apply=True,
    )
    job = Job(
        id="job-a",
        input_dir="/shared/input",
        output_dir="/shared/output",
        engine="dotsocr",
    )
    manifest = Manifest(
        id=5,
        job_id="job-a",
        input_mode="distributed_remote_folder_snapshot",
        manifest_path="/shared/manifest.jsonl",
        status="scanning",
    )

    monkeypatch.setattr(
        scheduling,
        "plan_scan_unit_completion",
        lambda *args, **kwargs: calls.append("plan") or plan,
    )
    monkeypatch.setattr(
        core,
        "get_job_or_raise",
        lambda *args, **kwargs: calls.append("job") or job,
    )
    monkeypatch.setattr(
        ports,
        "lock_manifest_for_scan_unit_completion",
        lambda *args, **kwargs: calls.append("manifest_lock") or manifest,
    )
    monkeypatch.setattr(
        scheduling,
        "apply_scan_unit_completion",
        lambda *args, **kwargs: calls.append("transition") or unit,
    )
    monkeypatch.setattr(
        ports,
        "materialize_scan_unit_completion",
        lambda *args, **kwargs: calls.append("materialize"),
    )
    monkeypatch.setattr(
        core,
        "freeze_manifest_if_scan_complete",
        lambda *args, **kwargs: calls.append("freeze"),
    )

    returned = core._complete_scan_unit(
        session,
        unit.id,
        ScanUnitCompleteRequest(
            assigned_server_id="server-a",
            attempt_count=2,
            child_paths=["/shared/input/child"],
            shards=[
                RemoteManifestShardRequest(
                    shard_index=1,
                    shard_path="/shared/shard.jsonl",
                    file_count=1,
                )
            ],
        ),
    )

    assert returned is unit
    assert calls == [
        "plan",
        "job",
        "manifest_lock",
        "transition",
        "materialize",
        "flush",
        "freeze",
    ]


def test_scan_failure_leaf_preserves_port_and_transition_order(
    monkeypatch,
) -> None:
    calls: list[str] = []
    session = _SequenceSession(calls)
    unit = _scan_unit()
    plan = scheduling.ScanUnitTransitionPlan(
        unit=unit,
        should_apply=True,
    )
    job = Job(
        id="job-a",
        input_dir="/shared/input",
        output_dir="/shared/output",
        engine="dotsocr",
    )

    monkeypatch.setattr(
        scheduling,
        "plan_scan_unit_failure",
        lambda *args, **kwargs: calls.append("plan") or plan,
    )
    monkeypatch.setattr(
        core,
        "get_job_or_raise",
        lambda *args, **kwargs: calls.append("job") or job,
    )
    monkeypatch.setattr(
        scheduling,
        "apply_scan_unit_failure",
        lambda *args, **kwargs: calls.append("transition") or unit,
    )
    monkeypatch.setattr(
        ports,
        "fail_manifest_if_scan_complete",
        lambda *args, **kwargs: calls.append("manifest_fail"),
    )

    returned = core._fail_scan_unit(
        session,
        unit.id,
        ScanUnitFailRequest(
            assigned_server_id="server-a",
            attempt_count=2,
            error_message="permission denied",
        ),
    )

    assert returned is unit
    assert calls == [
        "plan",
        "job",
        "transition",
        "flush",
        "manifest_fail",
    ]


def test_manifest_ports_do_not_own_scan_or_shard_status_assignment() -> None:
    ports_path = (
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
        / "ports.py"
    )
    tree = ast.parse(ports_path.read_text(encoding="utf-8"))
    status_assignments = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in (
            node.targets if isinstance(node, ast.Assign) else [node.target]
        )
        if isinstance(target, ast.Attribute)
        and target.attr == "status"
    ]

    assert status_assignments == []
