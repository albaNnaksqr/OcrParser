from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from tools import control_contracts


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = (
    ROOT
    / "tests"
    / "fixtures"
    / "contracts"
    / "control_scheduling_contracts.json"
)


def test_scheduling_contract_is_driven_by_real_service_calls() -> None:
    observed = control_contracts.build_scheduling_contract()
    expected = json.loads(FIXTURE.read_text(encoding="utf-8"))
    invariants = {
        item["id"]: item
        for item in observed["invariants"]
    }

    assert observed == expected
    assert observed["builder"] == (
        "tools.control_contracts.build_scheduling_contract"
    )
    assert invariants["claim_ordering"]["observed_claim_sequence"] == [
        {
            "shard_index": 1,
            "from_status": "pending",
            "attempt_count": 1,
        },
        {
            "shard_index": 1,
            "from_status": "retrying",
            "attempt_count": 2,
        },
        {
            "shard_index": 2,
            "from_status": "pending",
            "attempt_count": 1,
        },
        {
            "shard_index": 2,
            "from_status": "stale",
            "attempt_count": 2,
        },
        {
            "shard_index": 3,
            "from_status": "pending",
            "attempt_count": 1,
        },
    ]
    assert set(
        invariants["claim_ordering"]["claim_policy_sources"]
    ) == {
        "selector",
        "parent_select",
        "parent_lock",
        "compare_and_set_and_attempt",
        "attempt_snapshot",
    }
    assert all(
        source.startswith("ocr_platform/control/scheduling.py:")
        for source in invariants["claim_ordering"][
            "claim_policy_sources"
        ].values()
    )

    attempts = invariants["attempt_number_increment_and_uniqueness"]
    assert attempts["attempt_numbers_by_shard_index"] == {
        "1": [1, 2],
        "2": [1, 2],
        "3": [1],
    }
    assert attempts["unique_attempt_pairs"] is True

    fencing = invariants["server_and_attempt_fencing"]
    assert fencing["wrong_server_status"] == 409
    assert fencing["stale_attempt_status"] == 409
    assert fencing["old_terminal_attempt_status"] == 409

    restart_fencing = invariants[
        "server_reregistration_generation_fencing"
    ]
    assert restart_fencing["policy_source"].startswith(
        "ocr_platform/control/scheduling.py:"
    )
    assert restart_fencing["state_after_first_registration"] == {
        "shard": {
            "status": "stale",
            "assigned_server_id": None,
            "attempt_count": 2,
            "failure_category": "process_killed",
            "lease_cleared": True,
        },
        "attempts": [
            {
                "attempt_number": 1,
                "status": "failed",
                "failure_category": "model_error",
            },
            {
                "attempt_number": 2,
                "status": "stale",
                "failure_category": "process_killed",
            },
        ],
        "current_attempt_finished": True,
        "scan_unit": {
            "status": "stale",
            "assigned_server_id": None,
            "attempt_count": 1,
            "failure_category": "process_killed",
            "lease_cleared": True,
        },
        "job_status": "running",
    }
    assert restart_fencing["repeated_registration_unchanged"] is True

    terminal = invariants["terminal_monotonicity_and_replay"]
    assert terminal["terminal_response"] == {
        "status": "succeeded",
        "processed_files": 1,
    }
    assert terminal["same_terminal_replay"] == (
        terminal["terminal_response"]
    )
    assert terminal["late_nonterminal_replay"] == (
        terminal["terminal_response"]
    )
    assert terminal["persisted_state"] == {
        "status": "succeeded",
        "attempt_count": 2,
        "assigned_server_id": "worker-b",
        "processed_files": 1,
    }

    shard_lease = invariants["work_shard_lease_lifecycle"]
    assert shard_lease["heartbeat_extended_lease"] is True
    assert shard_lease["expired_status"] == "stale"
    assert shard_lease["reclaimed_status"] == "running"
    assert shard_lease["reclaimed_attempt_count"] == 2
    assert shard_lease["reclaimed_server_id"] == "worker-b"

    scan = invariants["scan_unit_claim_lease_and_fencing"]
    assert scan["first_claim"] == {
        "from_status": "pending",
        "attempt_count": 1,
        "server_id": "worker-a",
    }
    assert scan["heartbeat_extended_lease"] is True
    assert scan["reclaim"] == {
        "same_unit": True,
        "from_status": "stale",
        "attempt_count": 2,
        "server_id": "worker-b",
    }
    assert set(scan["claim_policy_sources"]) == {
        "selector",
        "compare_and_set",
    }
    assert all(
        source.startswith("ocr_platform/control/scheduling.py:")
        for source in scan["claim_policy_sources"].values()
    )
    assert scan["wrong_server_status"] == 409
    assert scan["stale_attempt_status"] == 409
    assert scan["old_terminal_attempt_status"] == 409

    scan_terminal = invariants["scan_unit_terminal_replay"]
    assert {
        key: scan_terminal[key]
        for key in (
            "success_status",
            "success_replay_status",
            "late_failure_status",
            "failure_status",
            "failure_replay_status",
            "late_completion_status",
        )
    } == {
        "success_status": "succeeded",
        "success_replay_status": "succeeded",
        "late_failure_status": 409,
        "failure_status": "failed",
        "failure_replay_status": "failed",
        "late_completion_status": 409,
    }

    stopped = invariants["stop_state_results"]
    assert stopped["policy_source"].startswith(
        "ocr_platform/control/scheduling.py:"
    )
    assert stopped["shards"] == {
        "before": {
            "1": "running",
            "2": "retrying",
            "3": "stale",
            "4": "pending",
        },
        "after_request_stop": {
            "1": "running",
            "2": "stopped",
            "3": "stopped",
            "4": "stopped",
        },
        "request_job_status": "stopping",
        "running_terminal_status": "stopped",
        "final_job_status": "stopped",
        "late_current_attempt_status": "stopped",
        "old_attempt_http_status": 409,
    }
    assert stopped["scan_units"] == {
        "before": {
            "scan-stop-scans": "running",
            "pending": "pending",
            "stale": "stale",
        },
        "after_request_stop": {
            "scan-stop-scans": "running",
            "pending": "stopped",
            "stale": "stopped",
        },
        "request_job_status": "stopping",
        "after_running_lease_expiry": {
            "scan-stop-scans": "stale",
            "pending": "stopped",
            "stale": "stopped",
        },
        "window_one_job_status": "stopping",
        "after_second_stop_sweep": {
            "scan-stop-scans": "stopped",
            "pending": "stopped",
            "stale": "stopped",
        },
        "window_two_job_status": "stopped",
    }

    recovery = invariants["recovery_finalization_bound"]
    assert recovery["work_shard_windows_to_terminal"] == 1
    assert recovery["scan_unit_windows_to_terminal"] == 2
    assert recovery["terminal_status"] == "stopped"

    postgres = observed["postgresql_concurrency_validation"]
    assert postgres["status"] == "external_required"
    assert postgres["executed_by_fixture_builder"] is False
    assert postgres["operator"] == "validation_operator"
    assert "test_postgres_migration_bridge.py" not in " ".join(
        postgres["supporting_non_runtime_checks"]
    )


@pytest.mark.parametrize(
    ("invariant_id", "field_path", "replacement"),
    [
        (
            "work_shard_lease_lifecycle",
            ("heartbeat_extended_lease",),
            False,
        ),
        (
            "claim_ordering",
            ("claim_policy_sources", "attempt_snapshot"),
            "ocr_platform/control/domains/manifests/core.py:1",
        ),
        (
            "scan_unit_claim_lease_and_fencing",
            ("reclaim", "attempt_count"),
            1,
        ),
        (
            "scan_unit_claim_lease_and_fencing",
            ("claim_policy_sources", "selector"),
            "ocr_platform/control/domains/manifests/core.py:1",
        ),
        (
            "server_reregistration_generation_fencing",
            ("repeated_registration_unchanged",),
            False,
        ),
        (
            "stop_state_results",
            ("scan_units", "window_two_job_status"),
            "stopping",
        ),
        (
            "recovery_finalization_bound",
            ("scan_unit_windows_to_terminal",),
            3,
        ),
    ],
)
def test_scheduling_validator_rejects_expected_value_mutations(
    invariant_id: str,
    field_path: tuple[str, ...],
    replacement: object,
) -> None:
    mutated = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))
    invariant = next(
        item
        for item in mutated["invariants"]
        if item["id"] == invariant_id
    )
    target = invariant
    for name in field_path[:-1]:
        target = target[name]
    target[field_path[-1]] = replacement

    with pytest.raises(ValueError):
        control_contracts.validate_scheduling_contract(mutated)


def test_scheduling_validator_rejects_missing_invariant() -> None:
    mutated = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))
    mutated["invariants"] = [
        item
        for item in mutated["invariants"]
        if item["id"] != "scan_unit_terminal_replay"
    ]

    with pytest.raises(ValueError, match="invariant set mismatch"):
        control_contracts.validate_scheduling_contract(mutated)


def test_scheduling_validator_keeps_postgres_concurrency_external() -> None:
    mutated = copy.deepcopy(json.loads(FIXTURE.read_text(encoding="utf-8")))
    mutated["postgresql_concurrency_validation"]["status"] = "covered"
    mutated["postgresql_concurrency_validation"][
        "executed_by_fixture_builder"
    ] = True

    with pytest.raises(ValueError, match="misrepresented"):
        control_contracts.validate_scheduling_contract(mutated)
