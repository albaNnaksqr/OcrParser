from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tools import control_contracts


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "contracts"


def test_control_contract_fixtures_match_without_rewriting() -> None:
    for name, payload in control_contracts.build_all_contracts().items():
        path = control_contracts.CONTRACT_FILES[name]
        assert path.read_text(encoding="utf-8") == (
            control_contracts.render_contract(name, payload)
        )


def test_contract_generation_is_byte_deterministic_in_memory() -> None:
    first = {
        name: control_contracts.render_contract(name, payload)
        for name, payload in control_contracts.build_all_contracts().items()
    }
    second = {
        name: control_contracts.render_contract(name, payload)
        for name, payload in control_contracts.build_all_contracts().items()
    }

    assert first == second


def test_contract_check_isolated_from_external_control_mode_environment() -> None:
    scenarios = [
        {
            "OCR_PLATFORM_REQUIRE_API_TOKEN": "1",
        },
        {
            "OCR_PLATFORM_REQUIRE_POSTGRES": "1",
            "OCR_PLATFORM_DATABASE_URL": (
                "postgresql+psycopg://contract.invalid/control"
            ),
        },
    ]
    for overrides in scenarios:
        environment = os.environ.copy()
        for name in control_contracts.CONTROL_CONTRACT_ENV_VARS:
            environment.pop(name, None)
        environment.update(overrides)
        completed = subprocess.run(
            [
                sys.executable,
                "tools/control_contracts.py",
                "check",
            ],
            cwd=ROOT,
            env=environment,
            check=False,
            capture_output=True,
            text=True,
        )

        assert completed.returncode == 0, (
            completed.stdout + completed.stderr
        )
        assert "Control contracts match checked-in baselines." in (
            completed.stdout
        )


def test_complete_canonical_openapi_baseline_is_locked() -> None:
    path = FIXTURES / "control_openapi.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    operations = {
        (path_name, method)
        for path_name, path_item in schema["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }

    assert len(schema["paths"]) == 48
    assert len(operations) == 50
    assert len(schema["components"]["schemas"]) == 60
    assert {
        "ModelProfileCertificationRequest",
        "ModelProfileCertificationResponse",
        "RiskAcceptanceRequest",
        "RiskAcceptanceResponse",
    } <= set(schema["components"]["schemas"])
    assert len(path.read_bytes()) == 86_912
    assert hashlib.sha256(path.read_bytes()).hexdigest() == (
        "1367f709c71ee670fd960cb88ff5c34890a1f7e316e401b56da3012de83b94ec"
    )


def test_api_route_traversal_covers_every_canonical_operation() -> None:
    from ocr_platform.control.app import create_app

    app = create_app()
    openapi = app.openapi()
    expected = {
        (path, method)
        for path, path_item in openapi["paths"].items()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    expected_ids = {
        path_item[method]["operationId"]
        for path_item in openapi["paths"].values()
        for method in path_item
        if method in {"get", "post", "put", "patch", "delete"}
    }
    routes = list(control_contracts._iter_api_routes(app.routes))
    actual = {
        (route.path, method.lower())
        for route in routes
        for method in route.methods
    }
    actual_ids = {
        route.unique_id
        for route in routes
        if route.include_in_schema
    }

    assert len(openapi["paths"]) == 48
    assert len(expected) == 50
    assert expected <= actual
    assert expected_ids == actual_ids
    assert "api_list_jobs_api_jobs_get" in actual_ids


def test_api_route_traversal_fallback_handles_nested_cycles() -> None:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/nested")
    def nested_endpoint() -> dict[str, bool]:
        return {"ok": True}

    nested = SimpleNamespace(routes=list(router.routes))
    root = SimpleNamespace(routes=[nested])
    root.routes.append(root)

    routes = list(
        control_contracts._iter_api_routes_fallback([root])
    )

    assert len(routes) == 1
    assert routes[0].route is router.routes[0]
    assert routes[0].endpoint is nested_endpoint
    assert routes[0].path == "/nested"
    assert routes[0].methods == frozenset({"GET"})
    assert routes[0].unique_id == router.routes[0].unique_id
    assert routes[0].include_in_schema is True


def test_api_route_traversal_fallback_handles_effective_contexts() -> None:
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/effective")
    def effective_endpoint() -> dict[str, bool]:
        return {"ok": True}

    route = router.routes[0]
    context = SimpleNamespace(
        original_route=route,
        endpoint=route.endpoint,
        path=route.path,
        methods=route.methods,
        unique_id=route.unique_id,
        include_in_schema=route.include_in_schema,
    )
    nested = SimpleNamespace(
        effective_route_contexts=lambda: iter([context])
    )

    routes = list(
        control_contracts._iter_api_routes_fallback([nested])
    )

    assert len(routes) == 1
    assert routes[0].route is route
    assert routes[0].endpoint is effective_endpoint
    assert routes[0].unique_id == route.unique_id


def test_http_behavior_contract_is_built_from_real_testclient_calls() -> None:
    fixture = json.loads(
        (FIXTURES / "control_http_behavior.json").read_text(encoding="utf-8")
    )
    observed = control_contracts.build_http_behavior_contract()
    scenarios = {
        item["scenario"]: item
        for item in observed["scenarios"]
    }

    assert observed == fixture
    assert observed["builder"] == (
        "tools.control_contracts.build_http_behavior_contract"
    )
    assert observed["schema_version"] == 2
    assert observed["covered_statuses"] == [
        200,
        400,
        401,
        403,
        404,
        409,
        422,
        503,
    ]
    assert scenarios["unauthorized_missing_api_token"]["stable_body"] == {
        "detail": "Missing or invalid API token"
    }
    assert scenarios["remote_admin_disabled"]["status"] == 403
    assert scenarios["migration_readiness_not_current"]["status"] == 503
    assert scenarios["preflight_is_report_not_transport_error"]["status"] == 200
    for scenario in scenarios.values():
        assert "stable_body" in scenario
        assert "normalization_rules" in scenario
        if scenario["status"] >= 300:
            if "shared_branch_authority" in scenario:
                assert "executed_branch" not in scenario
            else:
                assert "executed_branch" in scenario
                assert "shared_branch_authority" not in scenario
    validation = scenarios["validation_missing_server_fields"]["stable_body"]
    assert validation == {
        "detail": [
            {
                "input": {},
                "loc": ["body", "id"],
                "msg": "Field required",
                "type": "missing",
            },
            {
                "input": {},
                "loc": ["body", "name"],
                "msg": "Field required",
                "type": "missing",
            },
            {
                "input": {},
                "loc": ["body", "host"],
                "msg": "Field required",
                "type": "missing",
            },
        ]
    }
    readiness = scenarios["migration_readiness_not_current"]["stable_body"]
    assert readiness["ok"] is False
    assert readiness["service"] == "ocr-platform-control"
    assert readiness["database"]["is_current"] is False
    assert readiness["issues"]


def _scalar_paths(value: object, prefix: tuple[object, ...] = ()):
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _scalar_paths(item, prefix + (key,))
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            yield from _scalar_paths(item, prefix + (index,))
        return
    yield prefix


def _mutate_scalar(value: object) -> object:
    if value is None:
        return "mutated-null"
    if isinstance(value, bool):
        return not value
    if isinstance(value, int):
        return value + 1
    if isinstance(value, float):
        return value + 0.5
    if isinstance(value, str):
        return value + "-mutated"
    raise AssertionError(f"unsupported scalar type: {type(value)!r}")


def _replace_at_path(payload: object, path: tuple[object, ...]) -> None:
    parent = payload
    for part in path[:-1]:
        parent = parent[part]  # type: ignore[index]
    leaf = path[-1]
    parent[leaf] = _mutate_scalar(parent[leaf])  # type: ignore[index]


def test_http_stable_body_mutations_fail_the_fixture_comparison(
    tmp_path: Path,
) -> None:
    observed = control_contracts.build_http_behavior_contract()
    mutated_fixture = tmp_path / "mutated-http-contract.json"

    def require_check_failure(mutated: dict[str, object]) -> None:
        mutated_fixture.write_text(
            control_contracts.render_contract("http", mutated),
            encoding="utf-8",
        )
        assert not control_contracts.contract_file_matches(
            "http",
            observed,
            mutated_fixture,
        )

    mutated_scalars = 0
    for scenario_index, scenario in enumerate(observed["scenarios"]):
        body = scenario["stable_body"]
        scalar_paths = list(_scalar_paths(body))
        if not scalar_paths:
            mutated = copy.deepcopy(observed)
            mutated["scenarios"][scenario_index]["stable_body"] = [
                "unexpected-item"
            ]
            require_check_failure(mutated)
            continue
        for path in scalar_paths:
            mutated = copy.deepcopy(observed)
            if not path:
                mutated["scenarios"][scenario_index][
                    "stable_body"
                ] = "mutated-stable-body"
            else:
                _replace_at_path(
                    mutated["scenarios"][scenario_index]["stable_body"],
                    path,
                )
            require_check_failure(mutated)
            mutated_scalars += 1
    assert mutated_scalars > 100


def test_http_422_error_mutations_fail_without_deduplication(
    tmp_path: Path,
) -> None:
    observed = control_contracts.build_http_behavior_contract()
    mutated_fixture = tmp_path / "mutated-422-contract.json"

    def require_check_failure(mutated: dict[str, object]) -> None:
        mutated_fixture.write_text(
            control_contracts.render_contract("http", mutated),
            encoding="utf-8",
        )
        assert not control_contracts.contract_file_matches(
            "http",
            observed,
            mutated_fixture,
        )

    validation_index = next(
        index
        for index, scenario in enumerate(observed["scenarios"])
        if scenario["scenario"] == "validation_missing_server_fields"
    )
    errors = observed["scenarios"][validation_index]["stable_body"]["detail"]
    assert len(errors) == 3
    assert len({tuple(error["loc"]) for error in errors}) == 3
    for error_index in range(len(errors)):
        mutated = copy.deepcopy(observed)
        del mutated["scenarios"][validation_index]["stable_body"]["detail"][
            error_index
        ]
        require_check_failure(mutated)
        for field in ("loc", "type", "msg"):
            mutated = copy.deepcopy(observed)
            error = mutated["scenarios"][validation_index]["stable_body"][
                "detail"
            ][error_index]
            if field == "loc":
                error[field] = ["body", "mutated"]
            else:
                error[field] += "-mutated"
            require_check_failure(mutated)


def test_http_body_normalization_requires_explicit_field_rules() -> None:
    payload = {
        "request_id": "random-a",
        "nested": {"created_at": "2026-07-27T00:00:00Z"},
    }
    rules = (
        {
            "path": ["request_id"],
            "replacement": "<dynamic-request-id>",
            "reason": "generated request identifier",
        },
        {
            "path": ["nested", "created_at"],
            "replacement": "<dynamic-timestamp>",
            "reason": "response creation time",
        },
    )

    assert control_contracts._normalize_http_body(payload, rules) == {
        "request_id": "<dynamic-request-id>",
        "nested": {"created_at": "<dynamic-timestamp>"},
    }
    assert payload["request_id"] == "random-a"
    with pytest.raises(ValueError, match="scalar leaves only"):
        control_contracts._normalize_http_body(
            {"nested": {"created_at": "dynamic"}},
            (
                {
                    "path": ["nested"],
                    "replacement": "<dynamic-object>",
                    "reason": "invalid aggregate target",
                },
            ),
        )
    with pytest.raises(ValueError, match="replacements must be scalar"):
        control_contracts._normalize_http_body(
            {"request_id": "random-a"},
            (
                {
                    "path": ["request_id"],
                    "replacement": {"redacted": True},
                    "reason": "invalid aggregate replacement",
                },
            ),
        )
    with pytest.raises(ValueError, match="replacements must be scalar"):
        control_contracts._normalize_http_body(
            {"request_id": "random-a"},
            (
                {
                    "path": ["request_id"],
                    "replacement": ("not", "a", "scalar"),
                    "reason": "invalid tuple replacement",
                },
            ),
        )


def test_http_capture_side_scalar_mutation_fails_rendered_fixture_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = control_contracts._normalize_http_body

    def mutate_capture(payload, rules):
        captured = original(payload, rules)
        if (
            isinstance(captured, dict)
            and captured.get("service") == "ocr-platform-control"
        ):
            captured["service"] = "capture-mutated"
        return captured

    monkeypatch.setattr(
        control_contracts,
        "_normalize_http_body",
        mutate_capture,
    )
    observed = control_contracts.build_http_behavior_contract()
    assert not control_contracts.contract_file_matches(
        "http",
        observed,
        FIXTURES / "control_http_behavior.json",
    )


def test_http_capture_side_422_entry_mutation_fails_fixture_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = control_contracts._normalize_http_body

    def mutate_capture(payload, rules):
        captured = original(payload, rules)
        if (
            isinstance(captured, dict)
            and isinstance(captured.get("detail"), list)
            and len(captured["detail"]) == 3
            and all(
                isinstance(item, dict) and item.get("type") == "missing"
                for item in captured["detail"]
            )
        ):
            captured["detail"] = captured["detail"][:-1]
        return captured

    monkeypatch.setattr(
        control_contracts,
        "_normalize_http_body",
        mutate_capture,
    )
    observed = control_contracts.build_http_behavior_contract()
    assert not control_contracts.contract_file_matches(
        "http",
        observed,
        FIXTURES / "control_http_behavior.json",
    )


def test_http_operation_matrix_covers_every_openapi_operation_and_branch() -> None:
    matrix = json.loads(
        (
            FIXTURES / "control_http_operation_matrix.json"
        ).read_text(encoding="utf-8")
    )
    openapi = json.loads(
        (FIXTURES / "control_openapi.json").read_text(encoding="utf-8")
    )
    behavior = json.loads(
        (FIXTURES / "control_http_behavior.json").read_text(
            encoding="utf-8"
        )
    )
    expected = {
        schema["operationId"]: {
            "method": method.upper(),
            "path": path,
            "declared_statuses": sorted(
                int(status) if status.isdigit() else status
                for status in schema["responses"]
            ),
        }
        for path, path_item in openapi["paths"].items()
        for method, schema in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    operations = {
        operation["operation_id"]: operation
        for operation in matrix["operations"]
    }
    scenarios = {
        scenario["scenario"] for scenario in behavior["scenarios"]
    }

    assert set(operations) == set(expected)
    assert matrix["operation_count"] == 50
    assert matrix["non_2xx_branch_count"] == 201
    assert matrix["behavior_covered_branch_count"] == 153
    assert matrix["source_backed_exemption_count"] == 48
    for operation_id, operation in operations.items():
        assert operation["method"] == expected[operation_id]["method"]
        assert operation["path"] == expected[operation_id]["path"]
        assert [
            item["status"] for item in operation["declared_statuses"]
        ] == expected[operation_id]["declared_statuses"]
        for branch in operation["non_2xx_branches"]:
            assert branch["status"] in operation["source_reachable_statuses"]
            assert (
                (branch["behavior_reference"] is None)
                != (branch["exemption"] is None)
            )
            if branch["behavior_reference"] is not None:
                assert branch["behavior_reference"]["scenario"] in scenarios
                assert branch["behavior_reference"]["branch_id"] == (
                    branch["branch_id"]
                )
                assert branch["behavior_reference"]["source_evidence"] == (
                    branch["evidence"]
                )
                assert branch["behavior_reference"]["exception_types"] == (
                    branch["exception_types"]
                )
                if branch["behavior_reference"]["coverage"].startswith(
                    "shared"
                ):
                    assert branch["kind"] in {
                        "framework_request_validation",
                        "global_api_token_middleware",
                        "global_readiness_middleware",
                    }
                    assert "shared_authority" in (
                        branch["behavior_reference"]
                    )
                else:
                    assert branch["behavior_reference"]["coverage"] == (
                        "exact source branch"
                    )
                    assert branch["behavior_reference"][
                        "executed_branch"
                    ] == control_contracts._branch_identity(branch)
            else:
                assert branch["exemption"]["reason"]
                assert branch["exemption"]["evidence"] == [
                    branch["evidence"]
                ]
                if operation_id != "api_readyz_readyz_get":
                    assert operation_id in branch["exemption"]["reason"]
            if branch["kind"] == "framework_request_validation":
                assert branch["evidence"] == (
                    f"OpenAPI {operation['method']} "
                    f"{operation['path']} responses.422"
                )
            else:
                source_path, line = branch["evidence"].rsplit(":", 1)
                source_line = (
                    ROOT / source_path
                ).read_text(encoding="utf-8").splitlines()[int(line) - 1]
                assert (
                    "HTTPException" in source_line
                    or "JSONResponse" in source_line
                )


def test_http_operation_matrix_gate_rejects_missing_or_uncovered_entries() -> None:
    matrix = control_contracts.build_http_operation_matrix()
    expected_ids = {
        operation["operation_id"] for operation in matrix["operations"]
    }

    missing_operation = copy.deepcopy(matrix)
    missing_operation["operations"].pop()
    with pytest.raises(ValueError, match="coverage mismatch"):
        control_contracts.validate_http_operation_matrix(
            missing_operation,
            expected_ids,
        )

    uncovered = copy.deepcopy(matrix)
    branch = next(
        branch
        for operation in uncovered["operations"]
        for branch in operation["non_2xx_branches"]
    )
    branch["behavior_reference"] = None
    branch["exemption"] = None
    with pytest.raises(ValueError, match="exactly one coverage decision"):
        control_contracts.validate_http_operation_matrix(
            uncovered,
            expected_ids,
        )


def test_http_operation_matrix_source_inventory_has_expected_branch_kinds() -> None:
    matrix = control_contracts.build_http_operation_matrix()
    branches = [
        branch
        for operation in matrix["operations"]
        for branch in operation["non_2xx_branches"]
    ]
    by_kind: dict[str, int] = {}
    by_status: dict[int, int] = {}
    for branch in branches:
        by_kind[branch["kind"]] = by_kind.get(branch["kind"], 0) + 1
        by_status[branch["status"]] = by_status.get(branch["status"], 0) + 1

    assert by_kind == {
        "called_service_http_exception": 15,
        "explicit_response_status": 5,
        "framework_request_validation": 43,
        "global_api_token_middleware": 48,
        "global_readiness_middleware": 45,
        "router_exception_mapping": 45,
    }
    assert by_status == {
        400: 22,
        401: 48,
        403: 7,
        404: 25,
        409: 6,
        422: 43,
        503: 50,
    }
    scale_plan = next(
        operation
        for operation in matrix["operations"]
        if operation["operation_id"]
        == "api_remote_worker_scale_plan_api_remote_workers_scale_plan_post"
    )
    scale_400_chains = [
        branch["call_chain"]
        for branch in scale_plan["non_2xx_branches"]
        if branch["status"] == 400
    ]
    assert any(chain[-1].endswith(".validate_target") for chain in scale_400_chains)
    assert any(
        chain[-1].endswith(".validate_scale_request")
        for chain in scale_400_chains
    )


def test_http_operation_matrix_records_conditional_readiness_503() -> None:
    from ocr_platform.control.readiness import READINESS_ALLOWLIST

    matrix = control_contracts.build_http_operation_matrix()

    for operation in matrix["operations"]:
        branches = [
            branch
            for branch in operation["non_2xx_branches"]
            if branch["kind"] == "global_readiness_middleware"
        ]
        expected = (
            operation["path"].startswith("/api/")
            and operation["path"] not in READINESS_ALLOWLIST
        )
        assert len(branches) == int(expected), operation["operation_id"]
        if branches:
            reference = branches[0]["behavior_reference"]
            assert reference["scenario"] == "authorized_database_not_ready"
            assert reference["coverage"] == "shared middleware branch"


def test_readyz_behavior_binds_semantically_to_one_exact_503_branch() -> None:
    matrix = control_contracts.build_http_operation_matrix()
    readyz = next(
        operation
        for operation in matrix["operations"]
        if operation["operation_id"] == "api_readyz_readyz_get"
    )
    branches = [
        branch
        for branch in readyz["non_2xx_branches"]
        if branch["status"] == 503
    ]

    assert len(branches) == 2
    executed = [
        branch for branch in branches if branch["behavior_reference"] is not None
    ]
    defensive = [
        branch for branch in branches if branch["exemption"] is not None
    ]
    assert len(executed) == 1
    assert executed[0]["exception_types"] == []
    assert executed[0]["behavior_reference"]["coverage"] == (
        "exact source branch"
    )
    assert len(defensive) == 1
    assert defensive[0]["exception_types"] == ["Exception"]


def test_control_transport_future_gate_scans_all_runtime_python_sources() -> None:
    inventory = control_contracts.build_control_transport_inventory()
    expected_files = list(
        (ROOT / "ocr_platform" / "control").rglob("*.py")
    )

    assert inventory["scanned_file_count"] == len(expected_files)
    assert inventory["scanned_file_count"] >= 52
    assert len(inventory["branches"]) == 55
    assert inventory["forbidden_dependencies"] == []
    assert inventory["unresolved_status_calls"] == []
    matrix = control_contracts.build_http_operation_matrix()
    matrix_keys = {
        (branch["status"], branch["evidence"])
        for operation in matrix["operations"]
        for branch in operation["non_2xx_branches"]
        if branch["kind"] != "framework_request_validation"
    }
    inventory_keys = {
        (branch["status"], branch["evidence"])
        for branch in inventory["branches"]
    }
    assert inventory_keys == matrix_keys


def test_control_transport_future_gate_rejects_new_unmapped_transport() -> None:
    matrix = control_contracts.build_http_operation_matrix()
    expected_ids = {
        operation["operation_id"] for operation in matrix["operations"]
    }

    unmapped = copy.deepcopy(matrix)
    unmapped["transport_inventory"]["branches"].append(
        {
            "status": 418,
            "constructor": "HTTPException",
            "evidence": "ocr_platform/control/domains/jobs/core.py:1",
            "call_chain": ["future.application.command"],
        }
    )
    with pytest.raises(ValueError, match="inventory and operation matrix differ"):
        control_contracts.validate_http_operation_matrix(
            unmapped,
            expected_ids,
        )

    forbidden = copy.deepcopy(matrix)
    forbidden["transport_inventory"]["forbidden_dependencies"].append(
        {
            "source": "ocr_platform/control/domains/jobs/core.py:1",
            "dependency": "fastapi.HTTPException",
            "reason": "domain core cannot depend on transport",
        }
    )
    with pytest.raises(ValueError, match="transport dependencies"):
        control_contracts.validate_http_operation_matrix(
            forbidden,
            expected_ids,
        )

    wrong_branch = copy.deepcopy(matrix)
    exact_reference = next(
        branch["behavior_reference"]
        for operation in wrong_branch["operations"]
        for branch in operation["non_2xx_branches"]
        if branch["behavior_reference"] is not None
        and branch["behavior_reference"]["coverage"] == "exact source branch"
    )
    exact_reference["source_evidence"] = "wrong/source.py:1"
    with pytest.raises(ValueError, match="evidence is not exact"):
        control_contracts.validate_http_operation_matrix(
            wrong_branch,
            expected_ids,
        )


def test_database_metadata_and_migration_reference_are_locked() -> None:
    contract = json.loads(
        (FIXTURES / "control_database_metadata.json").read_text(
            encoding="utf-8"
        )
    )
    metadata = contract["orm_metadata"]
    sqlite_indexes = contract["sqlite_effective_indexes"]
    migration_reference = contract["migration_history_reference"]
    migration_path = ROOT / migration_reference["path"]
    migration_fixture = json.loads(migration_path.read_text(encoding="utf-8"))

    assert metadata["table_count"] == 12
    assert metadata["column_count"] == 187
    assert metadata["index_count"] == 15
    assert metadata["foreign_key_count"] == 12
    assert sqlite_indexes["index_count"] == 20
    assert sum(
        index["origin"] == "pk"
        for indexes in sqlite_indexes["tables"].values()
        for index in indexes
    ) == 5
    assert migration_reference["count"] == 20
    assert migration_reference["latest"] == (
        "0020_model_profile_certification"
    )
    assert migration_reference["sha256"] == hashlib.sha256(
        migration_path.read_bytes()
    ).hexdigest()
    assert list(migration_fixture["migrations"])[-1] == (
        migration_reference["latest"]
    )


def test_status_contract_distinguishes_closed_and_open_strings() -> None:
    contract = json.loads(
        (FIXTURES / "control_status_contracts.json").read_text(
            encoding="utf-8"
        )
    )
    entities = contract["entities"]

    assert contract["schema_version"] == 2
    assert contract["surface_count"] == 13
    assert entities["Job"]["openness"] == "closed_domain_set"
    assert entities["WorkShard"]["openness"] == "closed_wire_enum"
    assert entities["ShardAttempt"]["openness"] == "closed_derived_set"
    assert entities["ScanUnit"]["openness"] == "closed_domain_set"
    assert entities["Server"]["openness"] == "open_external_string"
    assert entities["JobFile"]["openness"] == "open_event_string"
    assert entities["JobEvent"]["openness"] == "open_event_string"
    assert entities["ManifestIntegrityResponse.status"]["openness"] == (
        "open_external_string"
    )
    assert entities["ModelProfileCertification.status"]["openness"] == (
        "closed_database_check"
    )
    scan_stopped_evidence = entities["ScanUnit"]["authority"][
        "value_evidence"
    ]["stopped"]
    assert {
        item["source"].split(":", 1)[0]
        for item in scan_stopped_evidence
    } == {"ocr_platform/control/scheduling.py"}
    scan_stale_evidence = entities["ScanUnit"]["authority"][
        "value_evidence"
    ]["stale"]
    assert {
        item["source"].split(":", 1)[0]
        for item in scan_stale_evidence
    } == {"ocr_platform/control/scheduling.py"}
    scan_running_evidence = entities["ScanUnit"]["authority"][
        "value_evidence"
    ]["running"]
    assert {
        item["source"].split(":", 1)[0]
        for item in scan_running_evidence
    } == {"ocr_platform/control/scheduling.py"}
    attempt_projection_evidence = [
        item
        for evidence in entities["ShardAttempt"]["authority"][
            "value_evidence"
        ].values()
        for item in evidence
        if item["symbol"] == "attempt.status = shard.status"
    ]
    assert attempt_projection_evidence
    assert {
        item["source"].split(":", 1)[0]
        for item in attempt_projection_evidence
    } == {"ocr_platform/control/scheduling.py"}


def test_every_closed_status_set_matches_independent_authority_evidence() -> None:
    contract = control_contracts.build_status_contract()
    closed_surfaces = {
        name: entity
        for name, entity in contract["entities"].items()
        if entity["openness"].startswith("closed_")
    }

    assert len(closed_surfaces) == 9
    for surface, entity in closed_surfaces.items():
        authority = entity["authority"]
        assert entity["values"] == authority["authoritative_values"]
        assert set(entity["values"]) == set(authority["value_evidence"])
        for value in entity["values"]:
            evidence = authority["value_evidence"][value]
            assert evidence, (surface, value)
            for item in evidence:
                source_path, line = item["source"].rsplit(":", 1)
                assert (ROOT / source_path).is_file()
                assert 1 <= int(line) <= len(
                    (ROOT / source_path)
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
        assert set(entity.get("behavior_observed_values", [])) <= set(
            entity["values"]
        )


def test_unobserved_terminal_and_projection_values_have_source_authority() -> None:
    entities = control_contracts.build_status_contract()["entities"]
    required = {
        "WorkShard": {"failed", "stopped"},
        "ShardAttempt": {"failed", "stopped"},
        "ScanUnit": {
            "failed",
            "pending",
            "running",
            "stale",
            "stopped",
            "succeeded",
        },
        "Manifest": {"failed", "ready", "scanning"},
        "Manifest.worker_integrity_status": {
            "failed",
            "ok",
            "pending",
            "running",
        },
        "ManifestFreezeReport.status": {
            "failed",
            "missing_manifest",
            "ready",
            "scanning",
        },
    }
    for surface, values in required.items():
        entity = entities[surface]
        assert values <= set(entity["values"])
        for value in values:
            assert entity["authority"]["value_evidence"][value]


def test_projection_authorities_are_ast_derived_relationships() -> None:
    entities = control_contracts.build_status_contract()["entities"]
    manifests_path = (
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
        / "freeze.py"
    )
    scheduling_path = (
        ROOT / "ocr_platform" / "control" / "scheduling.py"
    )
    attempt_matches = control_contracts._attribute_assignment_sources(
        scheduling_path,
        target=("attempt", "status"),
        value=("shard", "status"),
        expected_count=1,
    )
    freeze_matches = (
        control_contracts._constructor_keyword_projection_sources(
            manifests_path,
            constructor="ManifestFreezeReportResponse",
            keyword="status",
            value=("manifest", "status"),
            expected_count=2,
        )
    )

    attempt = entities["ShardAttempt"]["authority"][
        "projection_relationship"
    ]
    assert attempt == {
        "kind": "ast_attribute_assignment",
        "target": "attempt.status",
        "source": "shard.status",
        "matches": attempt_matches,
        "expected_count": 1,
    }

    freeze = entities["ManifestFreezeReport.status"]["authority"][
        "projection_relationship"
    ]
    assert freeze == {
        "kind": "ast_constructor_keyword",
        "constructor": "ManifestFreezeReportResponse",
        "keyword": "status",
        "source": "manifest.status",
        "matches": freeze_matches,
        "expected_count": 2,
        "projected_values": ["failed", "ready", "scanning"],
    }


def test_projection_ast_guards_reject_source_relationship_mutations(
    tmp_path: Path,
) -> None:
    manifests_source = (
        ROOT
        / "ocr_platform"
        / "control"
        / "domains"
        / "manifests"
        / "core.py"
    ).read_text(encoding="utf-8")
    scheduling_source = (
        ROOT / "ocr_platform" / "control" / "scheduling.py"
    ).read_text(encoding="utf-8")

    attempt_mutation = tmp_path / "attempt_projection.py"
    attempt_mutation.write_text(
        scheduling_source.replace(
            "attempt.status = shard.status",
            "attempt.status = request.status",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="attempt.status = shard.status"):
        control_contracts._attribute_assignment_sources(
            attempt_mutation,
            target=("attempt", "status"),
            value=("shard", "status"),
            expected_count=1,
        )

    freeze_mutation = tmp_path / "freeze_projection.py"
    freeze_mutation.write_text(
        manifests_source.replace(
            "status=manifest.status",
            'status="ready"',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError,
        match="ManifestFreezeReportResponse projections",
    ):
        control_contracts._constructor_keyword_projection_sources(
            freeze_mutation,
            constructor="ManifestFreezeReportResponse",
            keyword="status",
            value=("manifest", "status"),
            expected_count=2,
        )


def test_status_contract_mutations_fail_authority_validation() -> None:
    contract = control_contracts.build_status_contract()
    closed_surfaces = [
        name
        for name, entity in contract["entities"].items()
        if entity["openness"].startswith("closed_")
    ]
    for surface in closed_surfaces:
        original_values = contract["entities"][surface]["values"]

        deleted = copy.deepcopy(contract)
        deleted["entities"][surface]["values"] = original_values[1:]
        with pytest.raises(ValueError, match="differ from authority"):
            control_contracts.validate_status_contract(deleted)

        added = copy.deepcopy(contract)
        added["entities"][surface]["values"] = sorted(
            original_values + ["authority-mutation"]
        )
        with pytest.raises(ValueError, match="differ from authority"):
            control_contracts.validate_status_contract(added)

        changed = copy.deepcopy(contract)
        changed_values = list(original_values)
        changed_values[0] = changed_values[0] + "-mutated"
        changed["entities"][surface]["values"] = sorted(changed_values)
        with pytest.raises(ValueError, match="differ from authority"):
            control_contracts.validate_status_contract(changed)

    attempt_projection = copy.deepcopy(contract)
    attempt_projection["entities"]["ShardAttempt"]["authority"][
        "projection_relationship"
    ]["source"] = "request.status"
    with pytest.raises(ValueError, match="projection relationship source"):
        control_contracts.validate_status_contract(attempt_projection)

    freeze_projection = copy.deepcopy(contract)
    freeze_projection["entities"]["ManifestFreezeReport.status"][
        "authority"
    ]["projection_relationship"]["matches"].pop()
    with pytest.raises(ValueError, match="match count changed"):
        control_contracts.validate_status_contract(freeze_projection)


def test_open_status_surfaces_explain_why_they_are_not_exhaustive() -> None:
    entities = control_contracts.build_status_contract()["entities"]
    open_surfaces = {
        name: entity
        for name, entity in entities.items()
        if not entity["openness"].startswith("closed_")
    }

    assert set(open_surfaces) == {
        "JobEvent",
        "JobFile",
        "ManifestIntegrityResponse.status",
        "Server",
    }
    for entity in open_surfaces.values():
        assert "values" not in entity
        assert entity["known_values"]
        assert entity["open_reason"]
        assert entity["authority"]["sources"]
