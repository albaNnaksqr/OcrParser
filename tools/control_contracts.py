#!/usr/bin/env python3
"""Refresh or verify the checked-in v0.4 Control contract fixtures.

Tests only compare generated values with the reviewed fixtures. They never
rewrite files. Run ``python tools/control_contracts.py refresh`` explicitly,
review the resulting diff, and then run ``check``.
"""

from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import inspect
import json
import os
import re
import sys
import tempfile
import textwrap
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence, get_args
from unittest.mock import patch

from sqlalchemy import create_engine


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

CONTRACT_ROOT = ROOT / "tests" / "fixtures" / "contracts"
MIGRATION_FIXTURE = CONTRACT_ROOT / "control_migration_checksums.json"
CONTROL_CONTRACT_ENV_VARS = (
    "OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS",
    "OCR_PLATFORM_API_TOKEN",
    "OCR_PLATFORM_AUTO_MIGRATE",
    "OCR_PLATFORM_DATABASE_URL",
    "OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS",
    "OCR_PLATFORM_ENABLE_REMOTE_ADMIN",
    "OCR_PLATFORM_HOST",
    "OCR_PLATFORM_PORT",
    "OCR_PLATFORM_REQUIRE_API_TOKEN",
    "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS",
    "OCR_PLATFORM_REQUIRE_POSTGRES",
)
CONTRACT_FILES = {
    "openapi": CONTRACT_ROOT / "control_openapi.json",
    "http": CONTRACT_ROOT / "control_http_behavior.json",
    "http_operation_matrix": (
        CONTRACT_ROOT / "control_http_operation_matrix.json"
    ),
    "database": CONTRACT_ROOT / "control_database_metadata.json",
    "status": CONTRACT_ROOT / "control_status_contracts.json",
    "scheduling": CONTRACT_ROOT / "control_scheduling_contracts.json",
}


def canonical_json(payload: Any, *, compact: bool = False) -> str:
    options: dict[str, Any] = {
        "ensure_ascii": False,
        "sort_keys": True,
    }
    if compact:
        options["separators"] = (",", ":")
    else:
        options["indent"] = 2
        options["separators"] = (",", ": ")
    return json.dumps(payload, **options) + "\n"


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def build_openapi_contract() -> dict[str, Any]:
    with _temporary_environment(remove=CONTROL_CONTRACT_ENV_VARS):
        from ocr_platform.control.app import create_app

        # The JSON round trip applies the same recursive key ordering used by
        # the checked-in compact canonical representation.
        return json.loads(
            canonical_json(create_app().openapi(), compact=True)
        )


@contextmanager
def _temporary_environment(
    *,
    set_values: dict[str, str] | None = None,
    remove: tuple[str, ...] = (),
):
    names = set(remove) | set(set_values or {})
    previous = {name: os.environ.get(name) for name in names}
    try:
        for name in remove:
            os.environ.pop(name, None)
        for name, value in (set_values or {}).items():
            os.environ[name] = value
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@contextmanager
def _test_client():
    from fastapi.testclient import TestClient

    from ocr_platform.control.app import create_app
    from ocr_platform.control.database import create_session_factory, init_db

    with tempfile.TemporaryDirectory(prefix="ocr-control-contract-") as root:
        session_factory, engine = create_session_factory(
            f"sqlite:///{Path(root) / 'control.db'}"
        )
        init_db(engine)
        app = create_app(session_factory=session_factory)
        try:
            with TestClient(app) as client:
                yield client, session_factory, app
        finally:
            engine.dispose()


def _operation_id(app: Any, method: str, path: str) -> str:
    return str(app.openapi()["paths"][path][method.lower()]["operationId"])


@dataclass(frozen=True)
class _ApiRouteView:
    route: Any
    endpoint: Any
    path: str
    methods: frozenset[str]
    unique_id: str
    include_in_schema: bool


def _api_route_view(candidate: Any) -> _ApiRouteView | None:
    from fastapi.routing import APIRoute

    route = getattr(
        candidate,
        "route",
        getattr(candidate, "original_route", candidate),
    )
    if not isinstance(route, APIRoute):
        return None
    return _ApiRouteView(
        route=route,
        endpoint=getattr(candidate, "endpoint", route.endpoint),
        path=str(getattr(candidate, "path", route.path)),
        methods=frozenset(
            getattr(candidate, "methods", route.methods) or ()
        ),
        unique_id=str(
            getattr(candidate, "unique_id", route.unique_id)
        ),
        include_in_schema=bool(
            getattr(
                candidate,
                "include_in_schema",
                route.include_in_schema,
            )
        ),
    )


def _iter_api_routes_fallback(
    routes: Iterable[Any],
    *,
    _seen: set[int] | None = None,
) -> Iterator[_ApiRouteView]:
    """Recursively expand legacy or duck-typed nested route collections."""

    seen = _seen if _seen is not None else set()
    routes_id = id(routes)
    if routes_id in seen:
        return
    seen.add(routes_id)

    for candidate in routes:
        route = _api_route_view(candidate)
        if route is not None:
            yield route
            continue
        effective_contexts = getattr(
            candidate,
            "effective_route_contexts",
            None,
        )
        if callable(effective_contexts):
            yielded_context = False
            for context in effective_contexts():
                route = _api_route_view(context)
                if route is not None:
                    yielded_context = True
                    yield route
            if yielded_context:
                continue
        nested_routes = getattr(candidate, "routes", None)
        if nested_routes is not None:
            yield from _iter_api_routes_fallback(
                nested_routes,
                _seen=seen,
            )


def _iter_api_routes(routes: Sequence[Any]) -> Iterator[_ApiRouteView]:
    """Yield effective API routes across FastAPI's old and new layouts."""

    import fastapi.routing

    iterator = getattr(fastapi.routing, "iter_route_contexts", None)
    if iterator is None:
        yield from _iter_api_routes_fallback(routes)
        return

    for context in iterator(routes):
        route = _api_route_view(context)
        if route is not None:
            yield route


def _shape(value: Any) -> Any:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        if not value:
            return {"array_items": [], "length": 0}
        return {
            "array_items": [_shape(item) for item in value],
            "length": len(value),
        }
    if isinstance(value, dict):
        return {str(key): _shape(value[key]) for key in sorted(value)}
    return type(value).__name__


def _normalize_http_body(
    payload: Any,
    rules: tuple[dict[str, Any], ...],
) -> Any:
    """Normalize only explicitly named JSON fields in a captured response."""

    normalized = copy.deepcopy(payload)
    scalar_types = (str, int, float, bool, type(None))
    for rule in rules:
        path = rule.get("path")
        if (
            not isinstance(path, list)
            or not path
            or not all(isinstance(part, (str, int)) for part in path)
        ):
            raise ValueError(
                "HTTP body normalization rules require a non-empty path"
            )
        if "replacement" not in rule or not rule.get("reason"):
            raise ValueError(
                "HTTP body normalization rules require replacement and reason"
            )
        parent = normalized
        for part in path[:-1]:
            try:
                parent = parent[part]
            except (KeyError, IndexError, TypeError) as exc:
                raise ValueError(
                    f"HTTP body normalization path does not exist: {path!r}"
                ) from exc
        leaf = path[-1]
        try:
            leaf_value = parent[leaf]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(
                f"HTTP body normalization path does not exist: {path!r}"
            ) from exc
        if not isinstance(leaf_value, scalar_types):
            raise ValueError(
                "HTTP body normalization rules may target scalar leaves only"
            )
        replacement = rule["replacement"]
        if not isinstance(replacement, scalar_types):
            raise ValueError(
                "HTTP body normalization replacements must be scalar"
            )
        parent[leaf] = replacement
    return normalized


def _http_observation(
    *,
    scenario: str,
    app: Any,
    method: str,
    path_template: str,
    request_condition: str,
    response: Any,
    expected_status: int,
    normalization_rules: tuple[dict[str, Any], ...] = (),
    branch_selector: dict[str, Any] | None = None,
    note: str | None = None,
    response_format: str = "json",
) -> dict[str, Any]:
    if response.status_code != expected_status:
        raise RuntimeError(
            f"{scenario}: expected HTTP {expected_status}, got "
            f"{response.status_code}: {response.text}"
        )
    if response_format == "json":
        payload = response.json()
    elif response_format == "text":
        payload = response.text
    else:
        raise ValueError("unsupported HTTP response format")
    stable_body = _normalize_http_body(payload, normalization_rules)
    error_code = None
    if isinstance(payload, dict):
        if isinstance(payload.get("code"), str):
            error_code = payload["code"]
        detail = payload.get("detail")
        if isinstance(detail, dict) and isinstance(detail.get("code"), str):
            error_code = detail["code"]
    observed: dict[str, Any] = {
        "scenario": scenario,
        "operation_id": _operation_id(app, method, path_template),
        "method": method.upper(),
        "path": path_template,
        "request_condition": request_condition,
        "status": response.status_code,
        "body_shape": _shape(payload),
        "error_code": error_code,
        "stable_body": stable_body,
        "normalization_rules": [
            copy.deepcopy(rule) for rule in normalization_rules
        ],
    }
    if isinstance(stable_body, dict) and isinstance(
        stable_body.get("issues"), list
    ):
        observed["issue_codes"] = sorted(
            str(issue["code"])
            for issue in stable_body["issues"]
            if isinstance(issue, dict) and "code" in issue
        )
    if note is not None:
        observed["note"] = note
    if response_format == "text":
        observed["content_type"] = response.headers.get("content-type")
    if branch_selector is not None:
        observed["_branch_selector"] = copy.deepcopy(branch_selector)
    return observed


def build_http_behavior_contract() -> dict[str, Any]:
    """Exercise representative HTTP success and failure scenarios."""

    observations: list[dict[str, Any]] = []
    with _temporary_environment(remove=CONTROL_CONTRACT_ENV_VARS):
        with _test_client() as (client, _session_factory, app):
            response = client.get("/healthz")
            observations.append(
                _http_observation(
                    scenario="healthz_success",
                    app=app,
                    method="get",
                    path_template="/healthz",
                    request_condition="process is serving requests",
                    response=response,
                    expected_status=200,
                )
            )

            response = client.get("/api/servers")
            observations.append(
                _http_observation(
                    scenario="api_auth_disabled_success",
                    app=app,
                    method="get",
                    path_template="/api/servers",
                    request_condition="API token is not configured",
                    response=response,
                    expected_status=200,
                )
            )

            response = client.get("/api/system/metrics")
            observations.append(
                _http_observation(
                    scenario="system_metrics_success",
                    app=app,
                    method="get",
                    path_template="/api/system/metrics",
                    request_condition=(
                        "database snapshot is readable and API auth is disabled"
                    ),
                    response=response,
                    expected_status=200,
                    response_format="text",
                )
            )

            with patch(
                "ocr_platform.control.domains.diagnostics.router."
                "render_control_metrics",
                side_effect=RuntimeError("contract-private-database-error"),
            ):
                response = client.get("/api/system/metrics")
            observations.append(
                _http_observation(
                    scenario="system_metrics_database_unavailable",
                    app=app,
                    method="get",
                    path_template="/api/system/metrics",
                    request_condition=(
                        "metrics snapshot query fails before exposition"
                    ),
                    response=response,
                    expected_status=503,
                    branch_selector={
                        "kind": "explicit_response_status",
                        "exception_types": ["Exception"],
                    },
                )
            )

            response = client.get("/api/jobs?status=definitely-invalid")
            observations.append(
                _http_observation(
                    scenario="bad_request_invalid_job_status",
                    app=app,
                    method="get",
                    path_template="/api/jobs",
                    request_condition=(
                        "status filter is outside JOB_STATUS_FILTERS"
                    ),
                    response=response,
                    expected_status=400,
                )
            )

            response = client.get("/api/jobs/missing-job")
            observations.append(
                _http_observation(
                    scenario="not_found_unknown_job",
                    app=app,
                    method="get",
                    path_template="/api/jobs/{job_id}",
                    request_condition="job id does not exist",
                    response=response,
                    expected_status=404,
                )
            )

            response = client.post("/api/servers/register", json={})
            observations.append(
                _http_observation(
                    scenario="validation_missing_server_fields",
                    app=app,
                    method="post",
                    path_template="/api/servers/register",
                    request_condition=(
                        "required id, name, and host fields are missing"
                    ),
                    response=response,
                    expected_status=422,
                )
            )

            response = client.post(
                "/api/servers/register",
                json={
                    "id": "invalid-provenance-worker",
                    "name": "Invalid Provenance Worker",
                    "host": "localhost",
                    "capabilities": {
                        "engine_provenance": {
                            "profiles": {
                                "dotsocr": {
                                    "api_key": "contract-secret-not-stored",
                                }
                            }
                        }
                    },
                },
            )
            observations.append(
                _http_observation(
                    scenario="bad_request_invalid_register_provenance",
                    app=app,
                    method="post",
                    path_template="/api/servers/register",
                    request_condition=(
                        "engine provenance contains a non-whitelisted field"
                    ),
                    response=response,
                    expected_status=400,
                )
            )

            response = client.post(
                "/api/servers/invalid-provenance-worker/heartbeat",
                json={
                    "status": "idle",
                    "capabilities": {
                        "engine_provenance": {
                            "profiles": {
                                "dotsocr": {
                                    "runtime_digest": "not-a-sha256-digest",
                                }
                            }
                        }
                    },
                },
            )
            observations.append(
                _http_observation(
                    scenario="bad_request_invalid_heartbeat_provenance",
                    app=app,
                    method="post",
                    path_template="/api/servers/{server_id}/heartbeat",
                    request_condition=(
                        "engine provenance contains a malformed digest"
                    ),
                    response=response,
                    expected_status=400,
                )
            )

            from ocr_platform.control.models import (
                ModelProfile,
                ModelProfileCertification,
            )

            parser_revision = "contract-parser-revision"
            runtime_digest = f"sha256:{'a' * 64}"
            fixture_digest = f"sha256:{'b' * 64}"
            evidence_digest = f"sha256:{'c' * 64}"
            profile_cases = {
                "contract-cert-missing": {
                    "enforcement": "certified",
                    "status": "certified",
                    "parser_revision": parser_revision,
                    "model_revision": None,
                    "runtime_digest": runtime_digest,
                    "fixture_set_digest": fixture_digest,
                    "evidence_digest": evidence_digest,
                },
                "contract-cert-mismatch": {
                    "enforcement": "certified",
                    "status": "certified",
                    "parser_revision": parser_revision,
                    "model_revision": "model-r1",
                    "runtime_digest": runtime_digest,
                    "fixture_set_digest": fixture_digest,
                    "evidence_digest": evidence_digest,
                },
                "contract-cert-risk": {
                    "enforcement": "verified",
                    "status": "verified",
                    "risk_acceptance_json": "{}",
                },
            }
            with _session_factory() as session:
                for profile_id, certification in profile_cases.items():
                    session.add(
                        ModelProfile(
                            id=profile_id,
                            label="Contract certification profile",
                            engine="dotsocr",
                            model_name="DotsOCR",
                            extra_args_json="{}",
                        )
                    )
                    session.add(
                        ModelProfileCertification(
                            profile_id=profile_id,
                            **certification,
                        )
                    )
                session.commit()

            response = client.post(
                "/api/servers/register",
                json={
                    "id": "contract-cert-worker",
                    "name": "Contract Certification Worker",
                    "host": "localhost",
                    "capabilities": {
                        "shared_paths": [
                            {
                                "path": "/shared",
                                "exists": True,
                                "is_dir": True,
                                "readable": True,
                                "writable": True,
                            }
                        ],
                        "engine_provenance": {
                            "source_revision": parser_revision,
                            "dirty": False,
                            "profiles": {
                                "contract-cert-missing": {
                                    "model_revision": "model-r1",
                                    "runtime_digest": runtime_digest,
                                },
                                "contract-cert-mismatch": {
                                    "model_revision": "different-model",
                                    "runtime_digest": runtime_digest,
                                },
                            },
                        },
                    },
                },
            )
            if response.status_code != 200:
                raise RuntimeError(response.text)

            certification_scenarios = [
                (
                    "bad_request_model_profile_certification_missing",
                    "contract-cert-missing",
                    "certified profile provenance is incomplete",
                    "certification_policy.ModelProfileCertificationMissingError",
                ),
                (
                    "bad_request_model_profile_certification_mismatch",
                    "contract-cert-mismatch",
                    "certified profile and worker provenance differ",
                    "certification_policy.ModelProfileCertificationMismatchError",
                ),
                (
                    "bad_request_model_profile_risk_acceptance_required",
                    "contract-cert-risk",
                    "verified enforcement has no risk acceptance",
                    "certification_policy.ModelProfileRiskAcceptanceRequiredError",
                ),
            ]
            with patch(
                "ocr_platform.legal.build_provenance",
                return_value={
                    "source_revision": parser_revision,
                    "dirty": False,
                },
            ):
                for scenario, profile_id, condition, exception_type in (
                    certification_scenarios
                ):
                    response = client.post(
                        "/api/jobs",
                        json={
                            "input_dir": "/shared/input",
                            "output_dir": "/shared/output",
                            "engine": "dotsocr",
                            "model_profile_id": profile_id,
                            "assigned_server_id": "contract-cert-worker",
                        },
                    )
                    observations.append(
                        _http_observation(
                            scenario=scenario,
                            app=app,
                            method="post",
                            path_template="/api/jobs",
                            request_condition=condition,
                            response=response,
                            expected_status=400,
                            branch_selector={
                                "kind": "router_exception_mapping",
                                "exception_types": [exception_type],
                            },
                        )
                    )

            from ocr_platform.control.models import Server

            with _session_factory() as session:
                contract_worker = session.get(
                    Server,
                    "contract-cert-worker",
                )
                if contract_worker is not None:
                    session.delete(contract_worker)
                    session.commit()

            remote_admin_cases = [
                {
                    "scenario": "remote_admin_disabled",
                    "method": "post",
                    "path": "/api/remote-workers/preflight",
                    "json": {"host": "worker.example"},
                },
                {
                    "scenario": "remote_admin_targets_disabled",
                    "method": "get",
                    "path": "/api/remote-workers/targets",
                },
                {
                    "scenario": "remote_admin_install_dry_run_disabled",
                    "method": "post",
                    "path": "/api/remote-workers/install-dry-run",
                    "json": {
                        "host": "worker.example",
                        "server_id": "contract-worker",
                        "control_url": "http://control.example",
                    },
                },
                {
                    "scenario": "remote_admin_install_apply_disabled",
                    "method": "post",
                    "path": "/api/remote-workers/install-apply",
                    "json": {
                        "host": "worker.example",
                        "server_id": "contract-worker",
                        "control_url": "http://control.example",
                    },
                },
                {
                    "scenario": "remote_admin_scale_plan_disabled",
                    "method": "post",
                    "path": "/api/remote-workers/scale-plan",
                    "json": {"host": "worker.example"},
                },
                {
                    "scenario": "remote_admin_scale_apply_disabled",
                    "method": "post",
                    "path": "/api/remote-workers/scale-apply",
                    "json": {"host": "worker.example"},
                },
                {
                    "scenario": "remote_admin_service_disabled",
                    "method": "post",
                    "path": "/api/remote-workers/service",
                    "json": {
                        "host": "worker.example",
                        "action": "restart",
                    },
                },
            ]
            for case in remote_admin_cases:
                method = str(case["method"])
                path = str(case["path"])
                if method == "get":
                    response = client.get(path)
                else:
                    response = client.post(path, json=case["json"])
                observations.append(
                    _http_observation(
                        scenario=str(case["scenario"]),
                        app=app,
                        method=method,
                        path_template=path,
                        request_condition=(
                            "OCR_PLATFORM_ENABLE_REMOTE_ADMIN is not enabled"
                        ),
                        response=response,
                        expected_status=403,
                    )
                )

            register = client.post(
                "/api/servers/register",
                json={
                    "id": "contract-worker",
                    "name": "Contract Worker",
                    "host": "localhost",
                },
            )
            if register.status_code != 200:
                raise RuntimeError(register.text)
            create = client.post(
                "/api/jobs",
                json={
                    "input_dir": "/shared/input",
                    "output_dir": "/shared/output",
                    "engine": "dotsocr",
                    "input_mode": "directory",
                    "assigned_server_id": "contract-worker",
                },
            )
            if create.status_code != 200:
                raise RuntimeError(create.text)
            job_id = create.json()["id"]
            response = client.post(f"/api/jobs/{job_id}/archive")
            observations.append(
                _http_observation(
                    scenario="conflict_archive_nonterminal_job",
                    app=app,
                    method="post",
                    path_template="/api/jobs/{job_id}/archive",
                    request_condition="job is queued and nonterminal",
                    response=response,
                    expected_status=409,
                )
            )

            response = client.post(
                "/api/jobs/preflight",
                json={
                    "input_dir": "/shared/input",
                    "output_dir": "/shared/output",
                    "engine": "dotsocr",
                    "input_mode": "directory",
                    "assigned_server_id": "contract-worker",
                },
            )
            observations.append(
                _http_observation(
                    scenario="preflight_is_report_not_transport_error",
                    app=app,
                    method="post",
                    path_template="/api/jobs/preflight",
                    request_condition=(
                        "preflight may report issues in a 200 response body"
                    ),
                    response=response,
                    expected_status=200,
                    note=(
                        "At v0.3.2 preflight uses an ok/issues report and does "
                        "not return HTTP 503."
                    ),
                )
            )

            from fastapi.testclient import TestClient

            from ocr_platform.control.app import create_app

            strict_settings = replace(
                app.state.control_settings,
                require_current_migrations=True,
            )
            strict_app = create_app(
                session_factory=_session_factory,
                settings=strict_settings,
            )
            with TestClient(strict_app) as strict_client:
                response = strict_client.get("/readyz")
            observations.append(
                _http_observation(
                    scenario="migration_readiness_not_current",
                    app=app,
                    method="get",
                    path_template="/readyz",
                    request_condition=(
                        "strict current-migration policy observes a noncurrent "
                        "SQLite development schema after app construction"
                    ),
                    response=response,
                    expected_status=503,
                    branch_selector={
                        "kind": "explicit_response_status",
                        "exception_types": [],
                    },
                    note=(
                        "Strict production startup normally rejects this state "
                        "before serving; this scenario locks the existing "
                        "readiness response."
                    ),
                )
            )

    with _temporary_environment(
        set_values={"OCR_PLATFORM_API_TOKEN": "contract-runtime-secret"},
        remove=tuple(
            name
            for name in CONTROL_CONTRACT_ENV_VARS
            if name != "OCR_PLATFORM_API_TOKEN"
        ),
    ):
        with _test_client() as (client, _session_factory, app):
            response = client.get("/api/servers")
            observations.append(
                _http_observation(
                    scenario="unauthorized_missing_api_token",
                    app=app,
                    method="get",
                    path_template="/api/servers",
                    request_condition=(
                        "API token is configured but request has no credential"
                    ),
                    response=response,
                    expected_status=401,
                )
            )
            response = client.get(
                "/api/servers",
                headers={
                    "Authorization": "Bearer contract-runtime-secret",
                },
            )
            observations.append(
                _http_observation(
                    scenario="authorized_bearer_success",
                    app=app,
                    method="get",
                    path_template="/api/servers",
                    request_condition=(
                        "Bearer token matches configured API token"
                    ),
                    response=response,
                    expected_status=200,
                )
            )
            from ocr_platform.control.readiness import DatabaseReadiness

            with patch.object(
                app.state.database_readiness_probe,
                "check",
                return_value=DatabaseReadiness(
                    ready=False,
                    reason="migrations_pending",
                ),
            ):
                response = client.get(
                    "/api/servers",
                    headers={
                        "Authorization": "Bearer contract-runtime-secret",
                    },
                )
            observations.append(
                _http_observation(
                    scenario="authorized_database_not_ready",
                    app=app,
                    method="get",
                    path_template="/api/servers",
                    request_condition=(
                        "Bearer token is valid and PostgreSQL migrations "
                        "are pending"
                    ),
                    response=response,
                    expected_status=503,
                    branch_selector={
                        "kind": "global_readiness_middleware",
                    },
                    note=(
                        "Database readiness is a shared conditional transport "
                        "guard for non-allowlisted API operations."
                    ),
                )
            )

    observations = _bind_http_behavior_branches(observations)
    statuses = sorted({item["status"] for item in observations})
    required = [200, 400, 401, 403, 404, 409, 422, 503]
    if statuses != required:
        raise RuntimeError(
            f"HTTP behavior scenarios cover {statuses}, expected {required}"
        )
    return {
        "schema_version": 2,
        "builder": "tools.control_contracts.build_http_behavior_contract",
        "body_capture_policy": {
            "source": "response.json() from the recorded TestClient call",
            "normalization": (
                "scalar-leaf only; every scalar replacement requires an "
                "explicit path, replacement, and reason"
            ),
        },
        "error_envelope": {
            "http_exception": {"detail": "string"},
            "validation": {"detail": "array[validation-error]"},
            "application_error_code": "control_database_not_ready",
        },
        "covered_statuses": statuses,
        "scenarios": sorted(
            observations,
            key=lambda item: item["scenario"],
        ),
    }


def _call_terminal_name(call: ast.Call) -> str | None:
    if isinstance(call.func, ast.Name):
        return call.func.id
    if isinstance(call.func, ast.Attribute):
        return call.func.attr
    return None


def _integer_constants(node: ast.AST | None) -> list[int]:
    if node is None:
        return []
    return sorted(
        {
            item.value
            for item in ast.walk(node)
            if isinstance(item, ast.Constant)
            and isinstance(item.value, int)
            and not isinstance(item.value, bool)
        }
    )


def _exception_type_names(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Tuple):
        names: list[str] = []
        for item in node.elts:
            names.extend(_exception_type_names(item))
        return names
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        parts = [node.attr]
        value = node.value
        while isinstance(value, ast.Attribute):
            parts.append(value.attr)
            value = value.value
        if isinstance(value, ast.Name):
            parts.append(value.id)
        return [".".join(reversed(parts))]
    return [ast.unparse(node)]


class _StatusBranchVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        source_path: str,
        source_line_offset: int,
        call_chain: list[str],
        service_function: bool,
    ) -> None:
        self.source_path = source_path
        self.source_line_offset = source_line_offset
        self.call_chain = call_chain
        self.service_function = service_function
        self.exception_stack: list[list[str]] = []
        self.branches: list[dict[str, Any]] = []

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        self.exception_stack.append(_exception_type_names(node.type))
        for statement in node.body:
            self.visit(statement)
        self.exception_stack.pop()

    def visit_Call(self, node: ast.Call) -> None:
        terminal_name = _call_terminal_name(node)
        if terminal_name not in {"HTTPException", "JSONResponse"}:
            self.generic_visit(node)
            return
        status_expression = next(
            (
                keyword.value
                for keyword in node.keywords
                if keyword.arg == "status_code"
            ),
            None,
        )
        statuses = _integer_constants(status_expression)
        for status in statuses:
            exception_types = (
                self.exception_stack[-1] if self.exception_stack else []
            )
            if terminal_name == "JSONResponse":
                kind = "explicit_response_status"
            elif self.service_function:
                kind = "called_service_http_exception"
            elif exception_types:
                kind = "router_exception_mapping"
            else:
                kind = "direct_http_exception"
            evidence = (
                f"{self.source_path}:"
                f"{self.source_line_offset + node.lineno}"
            )
            self.branches.append(
                {
                    "status": status,
                    "kind": kind,
                    "evidence": evidence,
                    "exception_types": exception_types,
                    "call_chain": list(self.call_chain),
                }
            )
        self.generic_visit(node)


def _callable_source(
    function: Any,
) -> tuple[ast.AST, ast.AST, str, int]:
    source_lines, start_line = inspect.getsourcelines(function)
    source_path = Path(inspect.getsourcefile(function) or "")
    try:
        relative_path = source_path.resolve().relative_to(ROOT)
    except ValueError:
        relative_path = source_path
    tree = ast.parse(textwrap.dedent("".join(source_lines)))
    node = next(
        (
            item
            for item in tree.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            and item.name == function.__name__
        ),
        None,
    )
    if node is None:
        raise RuntimeError(f"cannot locate AST for {function!r}")
    return tree, node, relative_path.as_posix(), start_line - 1


def _resolve_called_service(function: Any, call: ast.Call) -> Any | None:
    target: Any = None
    if isinstance(call.func, ast.Name):
        target = function.__globals__.get(call.func.id)
    elif (
        isinstance(call.func, ast.Attribute)
        and isinstance(call.func.value, ast.Name)
    ):
        namespace = function.__globals__.get(call.func.value.id)
        target = getattr(namespace, call.func.attr, None)
    if not inspect.isfunction(target):
        return None
    module_name = str(getattr(target, "__module__", ""))
    if not module_name.startswith("ocr_platform.control."):
        return None
    if not module_name.endswith(".service"):
        return None
    return target


def _scan_callable_status_branches(function: Any) -> list[dict[str, Any]]:
    endpoint_label = f"{function.__module__}.{function.__name__}"
    branches: list[dict[str, Any]] = []
    visited: set[Any] = set()

    def scan(current: Any, call_chain: list[str]) -> None:
        if current in visited:
            return
        visited.add(current)
        _tree, node, source_path, line_offset = _callable_source(current)
        visitor = _StatusBranchVisitor(
            source_path=source_path,
            source_line_offset=line_offset,
            call_chain=call_chain,
            service_function=str(current.__module__).endswith(".service"),
        )
        visitor.visit(node)
        branches.extend(visitor.branches)
        for call in (
            item for item in ast.walk(node) if isinstance(item, ast.Call)
        ):
            target = _resolve_called_service(current, call)
            if target is None:
                continue
            scan(
                target,
                call_chain
                + [f"{target.__module__}.{target.__name__}"],
            )

    scan(function, [endpoint_label])
    unique = {
        canonical_json(branch, compact=True).strip(): branch
        for branch in branches
    }
    return sorted(
        unique.values(),
        key=lambda item: (
            item["status"],
            item["evidence"],
            item["kind"],
            item["call_chain"],
        ),
    )


def _global_guard_branch(
    *,
    status: int,
    kind: str,
) -> dict[str, Any]:
    path = ROOT / "ocr_platform" / "control" / "readiness.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    function = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == "api_token_auth"
        ),
        None,
    )
    if function is None:
        raise RuntimeError("cannot locate control request guard middleware")
    visitor = _StatusBranchVisitor(
        source_path=path.relative_to(ROOT).as_posix(),
        source_line_offset=0,
        call_chain=["ocr_platform.control.readiness.api_token_auth"],
        service_function=False,
    )
    visitor.visit(function)
    branches = [
        branch for branch in visitor.branches if branch["status"] == status
    ]
    if len(branches) != 1:
        raise RuntimeError(
            f"expected one control guard HTTP {status} branch, "
            f"found {len(branches)}"
        )
    branch = branches[0]
    branch["kind"] = kind
    return branch


def _global_auth_branch() -> dict[str, Any]:
    return _global_guard_branch(
        status=401,
        kind="global_api_token_middleware",
    )


def _global_readiness_branch() -> dict[str, Any]:
    return _global_guard_branch(
        status=503,
        kind="global_readiness_middleware",
    )


def _branch_identity(branch: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": int(branch["status"]),
        "kind": str(branch["kind"]),
        "evidence": str(branch["evidence"]),
        "exception_types": list(branch["exception_types"]),
        "call_chain": list(branch["call_chain"]),
    }


def _branch_matches_selector(
    branch: dict[str, Any],
    selector: dict[str, Any],
) -> bool:
    return all(branch.get(field) == value for field, value in selector.items())


def build_control_transport_inventory() -> dict[str, Any]:
    """Independently inventory transport dependencies and non-2xx builders."""

    control_root = ROOT / "ocr_platform" / "control"
    source_files = sorted(control_root.rglob("*.py"))
    branches: list[dict[str, Any]] = []
    forbidden_dependencies: list[dict[str, Any]] = []
    unresolved_status_calls: list[dict[str, Any]] = []
    for path in source_files:
        relative_path = path.relative_to(ROOT).as_posix()
        tree = ast.parse(path.read_text(encoding="utf-8"))
        parents = {
            child: parent
            for parent in ast.walk(tree)
            for child in ast.iter_child_nodes(parent)
        }
        transport_imports: list[tuple[int, str]] = []
        constructor_aliases: dict[str, str] = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (
                node.module == "fastapi"
                or str(node.module).startswith("fastapi.")
                or node.module == "starlette.responses"
            ):
                transport_imports.extend(
                    (
                        node.lineno,
                        f"{node.module}.{alias.name}",
                    )
                    for alias in node.names
                )
                constructor_aliases.update(
                    {
                        alias.asname or alias.name: alias.name
                        for alias in node.names
                    }
                )
            elif isinstance(node, ast.Import):
                transport_imports.extend(
                    (node.lineno, alias.name)
                    for alias in node.names
                    if alias.name == "fastapi"
                    or alias.name.startswith("fastapi.")
                )
        if path.name in {"core.py", "commands.py", "queries.py"}:
            forbidden_dependencies.extend(
                {
                    "source": f"{relative_path}:{line}",
                    "dependency": dependency,
                    "reason": (
                        "domain core/query/command cannot depend on FastAPI "
                        "transport"
                    ),
                }
                for line, dependency in transport_imports
            )

        for call in (
            node for node in ast.walk(tree) if isinstance(node, ast.Call)
        ):
            terminal_name = constructor_aliases.get(
                str(_call_terminal_name(call)),
                _call_terminal_name(call),
            )
            status_expression = next(
                (
                    keyword.value
                    for keyword in call.keywords
                    if keyword.arg == "status_code"
                ),
                None,
            )
            is_http_exception = terminal_name == "HTTPException"
            is_response = bool(
                terminal_name
                and (
                    terminal_name == "JSONResponse"
                    or terminal_name == "Response"
                    or terminal_name.endswith("Response")
                )
            )
            if not is_http_exception and not (
                is_response and status_expression is not None
            ):
                continue
            statuses = _integer_constants(status_expression)
            if not statuses:
                unresolved_status_calls.append(
                    {
                        "source": f"{relative_path}:{call.lineno}",
                        "constructor": terminal_name,
                        "reason": "status_code is not a static integer expression",
                    }
                )
                continue
            function_names: list[str] = []
            parent = parents.get(call)
            while parent is not None:
                if isinstance(
                    parent,
                    (ast.FunctionDef, ast.AsyncFunctionDef),
                ):
                    function_names.append(parent.name)
                parent = parents.get(parent)
            call_chain = [
                f"{relative_path}:{name}"
                for name in reversed(function_names)
            ]
            for status in statuses:
                if 200 <= status < 300:
                    continue
                branches.append(
                    {
                        "status": status,
                        "constructor": terminal_name,
                        "evidence": f"{relative_path}:{call.lineno}",
                        "call_chain": call_chain,
                    }
                )
    unique_branches = {
        (branch["status"], branch["evidence"]): branch
        for branch in branches
    }
    return {
        "scan_root": control_root.relative_to(ROOT).as_posix(),
        "scanned_file_count": len(source_files),
        "branches": sorted(
            unique_branches.values(),
            key=lambda item: (
                item["status"],
                item["evidence"],
                item["constructor"],
            ),
        ),
        "forbidden_dependencies": sorted(
            forbidden_dependencies,
            key=lambda item: (item["source"], item["dependency"]),
        ),
        "unresolved_status_calls": sorted(
            unresolved_status_calls,
            key=lambda item: (item["source"], item["constructor"]),
        ),
    }


def _bind_http_behavior_branches(
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind captured non-2xx behavior to a unique source branch."""

    from ocr_platform.control.app import create_app

    with _temporary_environment(remove=CONTROL_CONTRACT_ENV_VARS):
        app = create_app()
        openapi = app.openapi()
    operation_ids = {
        str(operation["operationId"])
        for path_item in openapi.get("paths", {}).values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    }
    endpoints_by_operation = {
        route.unique_id: route.endpoint
        for route in _iter_api_routes(app.routes)
        if route.include_in_schema and route.unique_id in operation_ids
    }

    auth_identity = _branch_identity(_global_auth_branch())
    readiness_identity = _branch_identity(_global_readiness_branch())
    bound = copy.deepcopy(observations)
    for observation in bound:
        selector = observation.pop("_branch_selector", None)
        status = int(observation["status"])
        if status < 300:
            if selector is not None:
                raise RuntimeError(
                    f"{observation['scenario']}: success cannot select a "
                    "non-2xx source branch"
                )
            continue
        if status == 401:
            observation["shared_branch_authority"] = {
                "mechanism": "global_api_token_middleware",
                "authority": auth_identity,
            }
            continue
        if (
            status == 503
            and selector is not None
            and selector.get("kind") == "global_readiness_middleware"
        ):
            observation["shared_branch_authority"] = {
                "mechanism": "global_readiness_middleware",
                "authority": readiness_identity,
            }
            continue
        if status == 422:
            observation["shared_branch_authority"] = {
                "mechanism": "fastapi_request_validation",
                "authority": {
                    "status": 422,
                    "kind": "framework_request_validation",
                    "exception_types": ["RequestValidationError"],
                    "call_chain": [
                        "fastapi.request_validation_exception_handler"
                    ],
                },
            }
            continue
        endpoint = endpoints_by_operation.get(observation["operation_id"])
        if endpoint is None:
            raise RuntimeError(
                f"{observation['scenario']}: no endpoint for operation "
                f"{observation['operation_id']}"
            )
        candidates = [
            branch
            for branch in _scan_callable_status_branches(endpoint)
            if int(branch["status"]) == status
        ]
        if selector is not None:
            candidates = [
                branch
                for branch in candidates
                if _branch_matches_selector(branch, selector)
            ]
        if len(candidates) != 1:
            raise RuntimeError(
                f"{observation['scenario']}: expected one exact HTTP {status} "
                f"branch, found {len(candidates)}; add a semantic selector"
            )
        observation["executed_branch"] = _branch_identity(candidates[0])
    return bound


def _behavior_reference(
    *,
    operation_id: str,
    branch: dict[str, Any],
    scenarios_by_operation_status: dict[
        tuple[str, int], list[dict[str, Any]]
    ],
) -> dict[str, Any] | None:
    status = int(branch["status"])
    kind = str(branch["kind"])
    if kind == "global_api_token_middleware":
        shared = [
            scenario
            for scenarios in scenarios_by_operation_status.values()
            for scenario in scenarios
            if scenario.get("shared_branch_authority", {}).get("mechanism")
            == "global_api_token_middleware"
        ]
        if not shared:
            return None
        return {
            "scenario": shared[0]["scenario"],
            "fixture": "control_http_behavior.json",
            "coverage": "shared middleware branch",
            "shared_authority": shared[0]["shared_branch_authority"],
            "branch_id": branch["branch_id"],
            "source_evidence": branch["evidence"],
            "exception_types": branch["exception_types"],
        }
    if kind == "global_readiness_middleware":
        shared = [
            scenario
            for scenarios in scenarios_by_operation_status.values()
            for scenario in scenarios
            if scenario.get("shared_branch_authority", {}).get("mechanism")
            == "global_readiness_middleware"
        ]
        if not shared:
            return None
        return {
            "scenario": shared[0]["scenario"],
            "fixture": "control_http_behavior.json",
            "coverage": "shared middleware branch",
            "shared_authority": shared[0]["shared_branch_authority"],
            "branch_id": branch["branch_id"],
            "source_evidence": branch["evidence"],
            "exception_types": branch["exception_types"],
        }
    if kind == "framework_request_validation":
        shared = [
            scenario
            for scenarios in scenarios_by_operation_status.values()
            for scenario in scenarios
            if scenario.get("shared_branch_authority", {}).get("mechanism")
            == "fastapi_request_validation"
        ]
        if not shared:
            return None
        return {
            "scenario": shared[0]["scenario"],
            "fixture": "control_http_behavior.json",
            "coverage": "shared FastAPI validation branch",
            "shared_authority": shared[0]["shared_branch_authority"],
            "branch_id": branch["branch_id"],
            "source_evidence": branch["evidence"],
            "exception_types": branch["exception_types"],
        }
    exact = scenarios_by_operation_status.get((operation_id, status), [])
    identity = _branch_identity(branch)
    exact = [
        scenario
        for scenario in exact
        if scenario.get("executed_branch") == identity
    ]
    if len(exact) > 1:
        raise RuntimeError(
            f"{operation_id} HTTP {status} has duplicate exact branch evidence"
        )
    if not exact:
        return None
    return {
        "scenario": exact[0]["scenario"],
        "fixture": "control_http_behavior.json",
        "coverage": "exact source branch",
        "branch_id": branch["branch_id"],
        "source_evidence": branch["evidence"],
        "exception_types": branch["exception_types"],
        "executed_branch": exact[0]["executed_branch"],
    }


def _branch_exemption(
    operation_id: str,
    branch: dict[str, Any],
) -> dict[str, Any]:
    status = int(branch["status"])
    kind = str(branch["kind"])
    evidence = str(branch["evidence"])
    exception_types = ", ".join(branch["exception_types"]) or "the branch"
    called_function = str(branch["call_chain"][-1])
    if (
        operation_id == "api_readyz_readyz_get"
        and kind == "explicit_response_status"
        and branch["exception_types"] == ["Exception"]
    ):
        reason = (
            "This defensive 503 requires system_diagnostics itself to raise "
            "an unexpected exception; the ordinary non-ready 503 branch is "
            "executed separately."
        )
    elif kind == "router_exception_mapping" and status == 400:
        reason = (
            f"{operation_id} has an invalid input/state branch requiring "
            f"{exception_types}; its distinct router mapping is frozen from "
            f"{evidence} but is not executed by the representative behavior "
            "set."
        )
    elif kind == "router_exception_mapping" and status == 404:
        reason = (
            f"{operation_id} has a missing job/shard/resource branch that "
            f"requires {exception_types}; its distinct router mapping is "
            f"frozen from {evidence} but is not separately executed."
        )
    elif kind == "router_exception_mapping" and status == 409:
        reason = (
            f"{operation_id} has a state, server, or attempt conflict that "
            f"requires {exception_types}; its distinct router mapping is "
            f"frozen from {evidence} but is not separately executed."
        )
    elif kind == "called_service_http_exception" and status == 400:
        reason = (
            f"{operation_id} requires Remote Admin to be enabled before "
            f"{called_function} can reject this exact target or scale-token "
            f"input; that service mapping is frozen from {evidence}."
        )
    else:
        reason = (
            f"This distinct status branch is source-reachable but is not "
            f"executed by the representative behavior set for {operation_id}."
        )
    return {
        "reason": reason,
        "evidence": [evidence],
    }


def validate_http_operation_matrix(
    matrix: dict[str, Any],
    expected_operation_ids: set[str],
) -> None:
    operations = matrix.get("operations", [])
    operation_ids = [
        str(operation.get("operation_id")) for operation in operations
    ]
    if len(operation_ids) != len(set(operation_ids)):
        raise ValueError("HTTP operation matrix contains duplicate operations")
    if set(operation_ids) != expected_operation_ids:
        missing = sorted(expected_operation_ids - set(operation_ids))
        unexpected = sorted(set(operation_ids) - expected_operation_ids)
        raise ValueError(
            f"HTTP operation coverage mismatch; missing={missing}, "
            f"unexpected={unexpected}"
        )
    branches = [
        branch
        for operation in operations
        for branch in operation.get("non_2xx_branches", [])
    ]
    for operation in operations:
        source_statuses = set(operation.get("source_reachable_statuses", []))
        for branch in operation.get("non_2xx_branches", []):
            if int(branch["status"]) < 300:
                raise ValueError(
                    f"{branch['branch_id']} is not a non-2xx branch"
                )
            if branch["status"] not in source_statuses:
                raise ValueError(
                    f"{branch['branch_id']} is absent from source statuses"
                )
            if (
                (branch.get("behavior_reference") is None)
                == (branch.get("exemption") is None)
            ):
                raise ValueError(
                    f"{branch['branch_id']} needs exactly one coverage decision"
                )
            reference = branch.get("behavior_reference")
            if reference is not None:
                if reference.get("branch_id") != branch["branch_id"]:
                    raise ValueError(
                        f"{branch['branch_id']} behavior uses another branch id"
                    )
                if reference.get("source_evidence") != branch["evidence"]:
                    raise ValueError(
                        f"{branch['branch_id']} behavior evidence is not exact"
                    )
                if reference.get("exception_types") != branch[
                    "exception_types"
                ]:
                    raise ValueError(
                        f"{branch['branch_id']} exception mapping is not exact"
                    )
                if reference.get("coverage", "").startswith("shared"):
                    if branch["kind"] not in {
                        "global_api_token_middleware",
                        "global_readiness_middleware",
                        "framework_request_validation",
                    }:
                        raise ValueError(
                            f"{branch['branch_id']} cannot share behavior"
                        )
                    if not reference.get("shared_authority"):
                        raise ValueError(
                            f"{branch['branch_id']} lacks shared authority"
                        )
                elif reference.get("executed_branch") != _branch_identity(
                    branch
                ):
                    raise ValueError(
                        f"{branch['branch_id']} behavior is not bound to its "
                        "exact source branch"
                    )
    behavior_count = sum(
        branch["behavior_reference"] is not None for branch in branches
    )
    exemption_count = len(branches) - behavior_count
    expected_counts = {
        "operation_count": len(operations),
        "non_2xx_branch_count": len(branches),
        "behavior_covered_branch_count": behavior_count,
        "source_backed_exemption_count": exemption_count,
    }
    for field, expected in expected_counts.items():
        if matrix.get(field) != expected:
            raise ValueError(
                f"HTTP operation matrix {field}={matrix.get(field)!r}, "
                f"expected {expected}"
            )
    inventory = matrix.get("transport_inventory", {})
    if inventory.get("forbidden_dependencies"):
        raise ValueError(
            "domain transport dependencies are forbidden: "
            f"{inventory['forbidden_dependencies']}"
        )
    if inventory.get("unresolved_status_calls"):
        raise ValueError(
            "transport status calls must be statically resolvable: "
            f"{inventory['unresolved_status_calls']}"
        )
    inventory_keys = {
        (int(branch["status"]), str(branch["evidence"]))
        for branch in inventory.get("branches", [])
    }
    matrix_keys = {
        (int(branch["status"]), str(branch["evidence"]))
        for branch in branches
        if branch["kind"] != "framework_request_validation"
    }
    if inventory_keys != matrix_keys:
        raise ValueError(
            "Control transport inventory and operation matrix differ; "
            f"inventory_only={sorted(inventory_keys - matrix_keys)}, "
            f"matrix_only={sorted(matrix_keys - inventory_keys)}"
        )


def build_http_operation_matrix() -> dict[str, Any]:
    """Inventory every canonical operation and every reachable status branch."""

    from ocr_platform.control.app import create_app
    from ocr_platform.control.readiness import READINESS_ALLOWLIST

    with _temporary_environment(remove=CONTROL_CONTRACT_ENV_VARS):
        app = create_app()
        openapi = app.openapi()
        behavior = build_http_behavior_contract()
    routes = list(_iter_api_routes(app.routes))
    route_index = {
        (route.path, method.lower()): route
        for route in routes
        for method in route.methods
    }
    scenarios_by_operation_status: dict[
        tuple[str, int], list[dict[str, Any]]
    ] = {}
    for scenario in behavior["scenarios"]:
        scenarios_by_operation_status.setdefault(
            (scenario["operation_id"], int(scenario["status"])),
            [],
        ).append(scenario)

    auth_branch = _global_auth_branch()
    readiness_branch = _global_readiness_branch()
    operations: list[dict[str, Any]] = []
    branch_count = 0
    behavior_count = 0
    exemption_count = 0
    for path, path_item in sorted(openapi["paths"].items()):
        for method, schema in sorted(path_item.items()):
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            operation_id = str(schema["operationId"])
            route = route_index.get((path, method))
            if route is None:
                raise RuntimeError(
                    f"OpenAPI operation has no APIRoute: {method} {path}"
                )
            declared_statuses = [
                {
                    "status": int(status) if status.isdigit() else status,
                    "description": response.get("description"),
                }
                for status, response in sorted(
                    schema.get("responses", {}).items()
                )
            ]
            branches = _scan_callable_status_branches(route.endpoint)
            if path.startswith("/api/"):
                branches.append(copy.deepcopy(auth_branch))
            if path.startswith("/api/") and path not in READINESS_ALLOWLIST:
                branches.append(copy.deepcopy(readiness_branch))
            if any(item["status"] == 422 for item in declared_statuses):
                branches.append(
                    {
                        "status": 422,
                        "kind": "framework_request_validation",
                        "evidence": (
                            f"OpenAPI {method.upper()} {path} responses.422"
                        ),
                        "exception_types": ["RequestValidationError"],
                        "call_chain": [
                            "fastapi.request_validation_exception_handler"
                        ],
                    }
                )
            branches = sorted(
                branches,
                key=lambda item: (
                    item["status"],
                    item["kind"],
                    item["evidence"],
                    item["call_chain"],
                ),
            )
            non_2xx_branches: list[dict[str, Any]] = []
            non_2xx_index = 0
            for source_branch in branches:
                status = int(source_branch["status"])
                if 200 <= status < 300:
                    continue
                non_2xx_index += 1
                branch = copy.deepcopy(source_branch)
                branch["branch_id"] = (
                    f"{operation_id}:{status}:{non_2xx_index:02d}"
                )
                behavior_reference = _behavior_reference(
                    operation_id=operation_id,
                    branch=branch,
                    scenarios_by_operation_status=(
                        scenarios_by_operation_status
                    ),
                )
                branch["behavior_reference"] = behavior_reference
                branch["exemption"] = (
                    None
                    if behavior_reference is not None
                    else _branch_exemption(operation_id, branch)
                )
                behavior_count += behavior_reference is not None
                exemption_count += behavior_reference is None
                non_2xx_branches.append(branch)
            branch_count += len(non_2xx_branches)
            source_statuses = sorted(
                {int(branch["status"]) for branch in branches}
            )
            scenario_refs = sorted(
                {
                    scenario["scenario"]
                    for (scenario_operation, _status), scenarios
                    in scenarios_by_operation_status.items()
                    if scenario_operation == operation_id
                    for scenario in scenarios
                }
            )
            operations.append(
                {
                    "operation_id": operation_id,
                    "method": method.upper(),
                    "path": path,
                    "declared_statuses": declared_statuses,
                    "source_reachable_statuses": source_statuses,
                    "behavior_scenario_refs": scenario_refs,
                    "non_2xx_branches": non_2xx_branches,
                }
            )

    if len(operations) != 50:
        raise RuntimeError(
            f"expected 50 canonical operations, found {len(operations)}"
        )
    matrix = {
        "schema_version": 1,
        "builder": "tools.control_contracts.build_http_operation_matrix",
        "operation_count": len(operations),
        "non_2xx_branch_count": branch_count,
        "behavior_covered_branch_count": behavior_count,
        "source_backed_exemption_count": exemption_count,
        "transport_inventory": build_control_transport_inventory(),
        "operations": sorted(
            operations,
            key=lambda item: item["operation_id"],
        ),
    }
    validate_http_operation_matrix(
        matrix,
        {
            str(schema["operationId"])
            for path_item in openapi["paths"].values()
            for method, schema in path_item.items()
            if method in {"get", "post", "put", "patch", "delete"}
        },
    )
    return matrix


def build_scheduling_contract() -> dict[str, Any]:
    """Exercise scheduling behavior through the real Control service boundary."""

    from datetime import timedelta

    from ocr_platform.control.models import (
        ScanUnit,
        ShardAttempt,
        WorkShard,
        utcnow,
    )

    def require_ok(response: Any, context: str) -> dict[str, Any]:
        if response.status_code != 200:
            raise RuntimeError(
                f"{context}: HTTP {response.status_code}: {response.text}"
            )
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError(f"{context}: expected an object response")
        return payload

    def heartbeat(
        client: Any,
        server_id: str,
        *,
        status: str = "idle",
        current_job_id: str | None = None,
    ) -> None:
        require_ok(
            client.post(
                f"/api/servers/{server_id}/heartbeat",
                json={
                    "status": status,
                    "current_job_id": current_job_id,
                    "capabilities": {
                        "shared_paths": [
                            {
                                "path": "/shared",
                                "exists": True,
                                "is_dir": True,
                                "readable": True,
                                "writable": True,
                            }
                        ]
                    },
                },
            ),
            f"heartbeat {server_id}",
        )

    def create_static_job(
        client: Any,
        *,
        suffix: str,
        shard_count: int,
        max_shard_attempts: int = 3,
    ) -> str:
        job = require_ok(
            client.post(
                "/api/jobs",
                json={
                    "input_dir": f"/shared/input-{suffix}",
                    "output_dir": f"/shared/output-{suffix}",
                    "engine": "dotsocr",
                    "input_mode": "remote_folder_snapshot",
                    "max_shard_attempts": max_shard_attempts,
                },
            ),
            f"create static job {suffix}",
        )
        job_id = str(job["id"])
        require_ok(
            client.post(
                f"/api/jobs/{job_id}/manifest",
                json={
                    "input_mode": "remote_folder_snapshot",
                    "input_root": f"/shared/input-{suffix}",
                    "manifest_path": (
                        f"/shared/contracts/{suffix}/manifest.jsonl"
                    ),
                    "file_count": shard_count,
                    "total_bytes": shard_count * 10,
                    "shards": [
                        {
                            "shard_index": index,
                            "shard_path": (
                                f"/shared/contracts/{suffix}/"
                                f"shard-{index:06d}.jsonl"
                            ),
                            "file_count": 1,
                        }
                        for index in range(1, shard_count + 1)
                    ],
                },
            ),
            f"register static manifest {suffix}",
        )
        return job_id

    def create_scan_job(client: Any, *, suffix: str) -> str:
        job = require_ok(
            client.post(
                "/api/jobs",
                json={
                    "input_dir": f"/shared/scan-{suffix}",
                    "output_dir": f"/shared/scan-output-{suffix}",
                    "engine": "dotsocr",
                    "input_mode": "distributed_remote_folder_snapshot",
                    "manifest_root": f"/shared/manifests/{suffix}",
                },
            ),
            f"create scan job {suffix}",
        )
        return str(job["id"])

    with _temporary_environment(remove=CONTROL_CONTRACT_ENV_VARS):
        with _test_client() as (client, session_factory, _app):
            heartbeat(client, "worker-a")
            heartbeat(client, "worker-b")
            job_id = create_static_job(
                client,
                suffix="ordering",
                shard_count=3,
            )
            require_ok(
                client.post("/api/agents/worker-a/next-job"),
                "claim scheduling contract job",
            )

            claim_sequence: list[dict[str, Any]] = []

            first = require_ok(
                client.post(
                    f"/api/jobs/{job_id}/shards/claim?server_id=worker-a"
                ),
                "claim first pending shard",
            )
            claim_sequence.append(
                {
                    "shard_index": first["shard_index"],
                    "from_status": "pending",
                    "attempt_count": first["attempt_count"],
                }
            )
            retrying = require_ok(
                client.post(
                    f"/api/shards/{first['id']}",
                    json={
                        "status": "failed",
                        "assigned_server_id": "worker-a",
                        "attempt_count": first["attempt_count"],
                        "failure_category": "model_unreachable",
                    },
                ),
                "move first shard to retrying",
            )
            if retrying["status"] != "retrying":
                raise RuntimeError(
                    "first failed attempt did not become retrying"
                )

            retry_claim = require_ok(
                client.post(
                    f"/api/jobs/{job_id}/shards/claim?server_id=worker-a"
                ),
                "reclaim retrying shard before pending work",
            )
            claim_sequence.append(
                {
                    "shard_index": retry_claim["shard_index"],
                    "from_status": retrying["status"],
                    "attempt_count": retry_claim["attempt_count"],
                }
            )
            require_ok(
                client.post(
                    f"/api/shards/{retry_claim['id']}",
                    json={
                        "status": "succeeded",
                        "assigned_server_id": "worker-a",
                        "attempt_count": retry_claim["attempt_count"],
                        "processed_files": 1,
                    },
                ),
                "finish reclaimed retrying shard",
            )

            second = require_ok(
                client.post(
                    f"/api/jobs/{job_id}/shards/claim?server_id=worker-a"
                ),
                "claim second pending shard",
            )
            claim_sequence.append(
                {
                    "shard_index": second["shard_index"],
                    "from_status": "pending",
                    "attempt_count": second["attempt_count"],
                }
            )
            with session_factory() as session:
                shard = session.get(WorkShard, second["id"])
                if shard is None or shard.lease_expires_at is None:
                    raise RuntimeError("claimed shard has no active lease")
                original_shard_lease = shard.lease_expires_at
            heartbeat(
                client,
                "worker-a",
                status="busy",
                current_job_id=job_id,
            )
            with session_factory() as session:
                shard = session.get(WorkShard, second["id"])
                if shard is None or shard.lease_expires_at is None:
                    raise RuntimeError("renewed shard has no active lease")
                renewed_shard_lease = shard.lease_expires_at
            shard_lease_extended = renewed_shard_lease > original_shard_lease

            with session_factory() as session:
                shard = session.get(WorkShard, second["id"])
                if shard is None:
                    raise RuntimeError("claimed shard disappeared")
                shard.lease_expires_at = utcnow() - timedelta(seconds=1)
                session.commit()

            summary = require_ok(
                client.get(f"/api/jobs/{job_id}/summary"),
                "reconcile expired shard lease",
            )
            if summary["stale_shards"] != 1:
                raise RuntimeError("expired shard was not reconciled to stale")

            stale_claim = require_ok(
                client.post(
                    f"/api/jobs/{job_id}/shards/claim?server_id=worker-b"
                ),
                "reclaim stale shard before pending work",
            )
            claim_sequence.append(
                {
                    "shard_index": stale_claim["shard_index"],
                    "from_status": "stale",
                    "attempt_count": stale_claim["attempt_count"],
                }
            )

            wrong_server = client.post(
                f"/api/shards/{stale_claim['id']}",
                json={
                    "status": "running",
                    "assigned_server_id": "worker-a",
                    "attempt_count": stale_claim["attempt_count"],
                    "processed_files": 99,
                },
            )
            stale_attempt = client.post(
                f"/api/shards/{stale_claim['id']}",
                json={
                    "status": "running",
                    "assigned_server_id": "worker-b",
                    "attempt_count": stale_claim["attempt_count"] - 1,
                    "processed_files": 99,
                },
            )
            if wrong_server.status_code != 409 or stale_attempt.status_code != 409:
                raise RuntimeError("server/attempt fencing did not return HTTP 409")

            terminal_request = {
                "status": "succeeded",
                "assigned_server_id": "worker-b",
                "attempt_count": stale_claim["attempt_count"],
                "processed_files": 1,
            }
            terminal = require_ok(
                client.post(
                    f"/api/shards/{stale_claim['id']}",
                    json=terminal_request,
                ),
                "complete current shard attempt",
            )
            repeated_terminal = require_ok(
                client.post(
                    f"/api/shards/{stale_claim['id']}",
                    json=terminal_request,
                ),
                "replay identical terminal update",
            )
            regressive = require_ok(
                client.post(
                    f"/api/shards/{stale_claim['id']}",
                    json={
                        "status": "running",
                        "assigned_server_id": "worker-b",
                        "attempt_count": stale_claim["attempt_count"],
                        "processed_files": 999,
                    },
                ),
                "replay late nonterminal update",
            )
            fenced_after_terminal = client.post(
                f"/api/shards/{stale_claim['id']}",
                json={
                    "status": "succeeded",
                    "assigned_server_id": "worker-a",
                    "attempt_count": stale_claim["attempt_count"] - 1,
                    "processed_files": 999,
                },
            )
            if fenced_after_terminal.status_code != 409:
                raise RuntimeError(
                    "old attempt was accepted after terminal completion"
                )

            third = require_ok(
                client.post(
                    f"/api/jobs/{job_id}/shards/claim?server_id=worker-a"
                ),
                "claim final pending shard",
            )
            claim_sequence.append(
                {
                    "shard_index": third["shard_index"],
                    "from_status": "pending",
                    "attempt_count": third["attempt_count"],
                }
            )

            with session_factory() as session:
                shards = (
                    session.query(WorkShard)
                    .filter_by(job_id=job_id)
                    .order_by(WorkShard.shard_index)
                    .all()
                )
                attempts = (
                    session.query(ShardAttempt)
                    .filter_by(job_id=job_id)
                    .order_by(
                        ShardAttempt.shard_id,
                        ShardAttempt.attempt_number,
                    )
                    .all()
                )
                index_by_id = {
                    int(shard.id): int(shard.shard_index)
                    for shard in shards
                }
                attempt_numbers: dict[str, list[int]] = {}
                attempt_statuses: dict[str, list[str]] = {}
                for attempt in attempts:
                    key = str(index_by_id[int(attempt.shard_id)])
                    attempt_numbers.setdefault(key, []).append(
                        int(attempt.attempt_number)
                    )
                    attempt_statuses.setdefault(key, []).append(
                        str(attempt.status)
                    )
                persisted_second = next(
                    shard
                    for shard in shards
                    if int(shard.shard_index) == 2
                )
                current_state = {
                    "status": persisted_second.status,
                    "attempt_count": persisted_second.attempt_count,
                    "assigned_server_id": (
                        persisted_second.assigned_server_id
                    ),
                    "processed_files": persisted_second.processed_files,
                }
                unique_attempt_pairs = len(
                    {
                        (
                            int(attempt.shard_id),
                            int(attempt.attempt_number),
                        )
                        for attempt in attempts
                    }
                ) == len(attempts)

            scan_job_id = create_scan_job(client, suffix="lease")
            failed_scan_job_id = create_scan_job(client, suffix="failure")
            scan_first = require_ok(
                client.post("/api/scan-units/claim?server_id=worker-a"),
                "claim first pending scan unit",
            )
            if scan_first["job_id"] != scan_job_id:
                raise RuntimeError("scan claim order did not use lowest unit id")
            with session_factory() as session:
                scan_unit = session.get(ScanUnit, scan_first["id"])
                if scan_unit is None or scan_unit.lease_expires_at is None:
                    raise RuntimeError("claimed scan unit has no active lease")
                original_scan_lease = scan_unit.lease_expires_at
            heartbeat(
                client,
                "worker-a",
                status="busy",
                current_job_id=scan_job_id,
            )
            with session_factory() as session:
                scan_unit = session.get(ScanUnit, scan_first["id"])
                if scan_unit is None or scan_unit.lease_expires_at is None:
                    raise RuntimeError("renewed scan unit has no active lease")
                renewed_scan_lease = scan_unit.lease_expires_at
                scan_unit.lease_expires_at = utcnow() - timedelta(seconds=1)
                session.commit()
            scan_lease_extended = renewed_scan_lease > original_scan_lease

            scan_reclaim = require_ok(
                client.post("/api/scan-units/claim?server_id=worker-b"),
                "reclaim stale scan unit before pending scan unit",
            )
            if scan_reclaim["id"] != scan_first["id"]:
                raise RuntimeError("stale scan unit was not recovery-prioritized")
            scan_wrong_server = client.post(
                f"/api/scan-units/{scan_reclaim['id']}/complete",
                json={
                    "assigned_server_id": "worker-a",
                    "attempt_count": scan_reclaim["attempt_count"],
                    "file_count": 0,
                    "total_bytes": 0,
                    "child_paths": [],
                    "shards": [],
                },
            )
            scan_stale_attempt = client.post(
                f"/api/scan-units/{scan_reclaim['id']}/complete",
                json={
                    "assigned_server_id": "worker-b",
                    "attempt_count": scan_reclaim["attempt_count"] - 1,
                    "file_count": 0,
                    "total_bytes": 0,
                    "child_paths": [],
                    "shards": [],
                },
            )
            if (
                scan_wrong_server.status_code != 409
                or scan_stale_attempt.status_code != 409
            ):
                raise RuntimeError("scan server/attempt fencing did not hold")
            scan_success_request = {
                "assigned_server_id": "worker-b",
                "attempt_count": scan_reclaim["attempt_count"],
                "manifest_path": "/shared/manifests/lease/manifest.jsonl",
                "file_count": 0,
                "total_bytes": 0,
                "child_paths": [],
                "shards": [],
            }
            scan_success = require_ok(
                client.post(
                    f"/api/scan-units/{scan_reclaim['id']}/complete",
                    json=scan_success_request,
                ),
                "complete reclaimed scan unit",
            )
            scan_success_replay = require_ok(
                client.post(
                    f"/api/scan-units/{scan_reclaim['id']}/complete",
                    json=scan_success_request,
                ),
                "replay successful scan completion",
            )
            scan_late_fail = client.post(
                f"/api/scan-units/{scan_reclaim['id']}/fail",
                json={
                    "assigned_server_id": "worker-b",
                    "attempt_count": scan_reclaim["attempt_count"],
                    "failure_category": "model_error",
                    "error_message": "late failure",
                },
            )
            scan_old_attempt = client.post(
                f"/api/scan-units/{scan_reclaim['id']}/complete",
                json=scan_success_request
                | {"attempt_count": scan_reclaim["attempt_count"] - 1},
            )

            failed_scan = require_ok(
                client.post("/api/scan-units/claim?server_id=worker-a"),
                "claim pending scan unit for failure replay",
            )
            if failed_scan["job_id"] != failed_scan_job_id:
                raise RuntimeError("unexpected scan unit claimed for failure")
            scan_failure_request = {
                "assigned_server_id": "worker-a",
                "attempt_count": failed_scan["attempt_count"],
                "failure_category": "model_unreachable",
                "error_message": "fixed contract failure",
            }
            scan_failed = require_ok(
                client.post(
                    f"/api/scan-units/{failed_scan['id']}/fail",
                    json=scan_failure_request,
                ),
                "fail scan unit",
            )
            scan_failed_replay = require_ok(
                client.post(
                    f"/api/scan-units/{failed_scan['id']}/fail",
                    json=scan_failure_request,
                ),
                "replay failed scan unit",
            )
            scan_late_complete = client.post(
                f"/api/scan-units/{failed_scan['id']}/complete",
                json={
                    "assigned_server_id": "worker-a",
                    "attempt_count": failed_scan["attempt_count"],
                    "file_count": 0,
                    "total_bytes": 0,
                    "child_paths": [],
                    "shards": [],
                },
            )

            stop_shard_job_id = create_static_job(
                client,
                suffix="stop-shards",
                shard_count=4,
            )
            require_ok(
                client.post("/api/agents/worker-a/next-job"),
                "claim shard stop job",
            )
            stop_running_shard = require_ok(
                client.post(
                    f"/api/jobs/{stop_shard_job_id}/shards/claim"
                    "?server_id=worker-a"
                ),
                "claim running shard for stop",
            )
            with session_factory() as session:
                stop_shards = (
                    session.query(WorkShard)
                    .filter_by(job_id=stop_shard_job_id)
                    .order_by(WorkShard.shard_index)
                    .all()
                )
                stop_shards[1].status = "retrying"
                stop_shards[1].attempt_count = 1
                stop_shards[2].status = "stale"
                stop_shards[2].attempt_count = 1
                before_shard_stop = {
                    str(shard.shard_index): shard.status
                    for shard in stop_shards
                }
                session.commit()
            stop_shard_response = require_ok(
                client.post(
                    f"/api/jobs/{stop_shard_job_id}/request-stop"
                ),
                "request stop for mixed shard states",
            )
            with session_factory() as session:
                after_stop_shards = (
                    session.query(WorkShard)
                    .filter_by(job_id=stop_shard_job_id)
                    .order_by(WorkShard.shard_index)
                    .all()
                )
                after_shard_stop = {
                    str(shard.shard_index): shard.status
                    for shard in after_stop_shards
                }
            stopped_running_shard = require_ok(
                client.post(
                    f"/api/shards/{stop_running_shard['id']}",
                    json={
                        "status": "stopped",
                        "assigned_server_id": "worker-a",
                        "attempt_count": (
                            stop_running_shard["attempt_count"]
                        ),
                        "failure_category": "operator_stopped",
                    },
                ),
                "stop running shard",
            )
            stop_shard_summary = require_ok(
                client.get(f"/api/jobs/{stop_shard_job_id}/summary"),
                "finalize stopped shard job",
            )
            stop_shard_late_current = require_ok(
                client.post(
                    f"/api/shards/{stop_running_shard['id']}",
                    json={
                        "status": "running",
                        "assigned_server_id": "worker-a",
                        "attempt_count": (
                            stop_running_shard["attempt_count"]
                        ),
                        "processed_files": 99,
                    },
                ),
                "late current shard update after stop",
            )
            stop_shard_old_attempt = client.post(
                f"/api/shards/{stop_running_shard['id']}",
                json={
                    "status": "running",
                    "assigned_server_id": "worker-a",
                    "attempt_count": 0,
                    "processed_files": 99,
                },
            )

            stop_scan_job_id = create_scan_job(
                client,
                suffix="stop-scans",
            )
            stop_running_scan = require_ok(
                client.post("/api/scan-units/claim?server_id=worker-a"),
                "claim running scan unit for stop",
            )
            if stop_running_scan["job_id"] != stop_scan_job_id:
                raise RuntimeError("unexpected scan unit claimed for stop")
            with session_factory() as session:
                session.add_all(
                    [
                        ScanUnit(
                            job_id=stop_scan_job_id,
                            path="/shared/scan-stop-scans/pending",
                            status="pending",
                        ),
                        ScanUnit(
                            job_id=stop_scan_job_id,
                            path="/shared/scan-stop-scans/stale",
                            status="stale",
                            attempt_count=1,
                            failure_category="lease_expired",
                        ),
                    ]
                )
                session.commit()
                before_scan_stop = {
                    unit.path.rsplit("/", 1)[-1]: unit.status
                    for unit in (
                        session.query(ScanUnit)
                        .filter_by(job_id=stop_scan_job_id)
                        .order_by(ScanUnit.id)
                        .all()
                    )
                }
            stop_scan_first = require_ok(
                client.post(f"/api/jobs/{stop_scan_job_id}/request-stop"),
                "request stop for mixed scan unit states",
            )
            with session_factory() as session:
                after_first_scan_stop = {
                    unit.path.rsplit("/", 1)[-1]: unit.status
                    for unit in (
                        session.query(ScanUnit)
                        .filter_by(job_id=stop_scan_job_id)
                        .order_by(ScanUnit.id)
                        .all()
                    )
                }
                running = session.get(
                    ScanUnit,
                    stop_running_scan["id"],
                )
                if running is None:
                    raise RuntimeError("running stop scan unit disappeared")
                running.lease_expires_at = utcnow() - timedelta(seconds=1)
                session.commit()
            stop_scan_window_one = require_ok(
                client.get(f"/api/jobs/{stop_scan_job_id}/summary"),
                "first scan recovery window",
            )
            with session_factory() as session:
                after_scan_expiry = {
                    unit.path.rsplit("/", 1)[-1]: unit.status
                    for unit in (
                        session.query(ScanUnit)
                        .filter_by(job_id=stop_scan_job_id)
                        .order_by(ScanUnit.id)
                        .all()
                    )
                }
            stop_scan_window_two = require_ok(
                client.post(f"/api/jobs/{stop_scan_job_id}/request-stop"),
                "second scan recovery window",
            )
            with session_factory() as session:
                after_second_scan_stop = {
                    unit.path.rsplit("/", 1)[-1]: unit.status
                    for unit in (
                        session.query(ScanUnit)
                        .filter_by(job_id=stop_scan_job_id)
                        .order_by(ScanUnit.id)
                        .all()
                    )
                }

    expected_claim_sequence = [
        {"shard_index": 1, "from_status": "pending", "attempt_count": 1},
        {"shard_index": 1, "from_status": "retrying", "attempt_count": 2},
        {"shard_index": 2, "from_status": "pending", "attempt_count": 1},
        {"shard_index": 2, "from_status": "stale", "attempt_count": 2},
        {"shard_index": 3, "from_status": "pending", "attempt_count": 1},
    ]
    if claim_sequence != expected_claim_sequence:
        raise RuntimeError(
            f"unexpected claim sequence: {claim_sequence}"
        )
    if attempt_numbers != {"1": [1, 2], "2": [1, 2], "3": [1]}:
        raise RuntimeError(f"unexpected attempt numbers: {attempt_numbers}")
    if not unique_attempt_pairs:
        raise RuntimeError("duplicate shard/attempt-number pair observed")
    expected_terminal = {
        "status": "succeeded",
        "attempt_count": 2,
        "assigned_server_id": "worker-b",
        "processed_files": 1,
    }
    if current_state != expected_terminal:
        raise RuntimeError(
            f"terminal state regressed after replay: {current_state}"
        )
    for payload in (terminal, repeated_terminal, regressive):
        if (
            payload["status"] != "succeeded"
            or payload["processed_files"] != 1
        ):
            raise RuntimeError("terminal replay was not idempotent")

    contract = {
        "schema_version": 2,
        "builder": "tools.control_contracts.build_scheduling_contract",
        "scope": [
            "work_shard_claim_attempt_lease_and_fencing",
            "scan_unit_claim_attempt_lease_and_fencing",
            "terminal_replay",
            "stop_and_recovery_finalization",
        ],
        "invariants": [
            {
                "id": "claim_ordering",
                "contract": (
                    "retrying and stale work is reclaimed before pending "
                    "work; equal-priority pending work uses shard_index order"
                ),
                "observed_claim_sequence": claim_sequence,
                "evidence": (
                    "tests/test_control_scheduling_contracts.py::"
                    "test_scheduling_contract_is_driven_by_real_service_calls"
                ),
            },
            {
                "id": "attempt_number_increment_and_uniqueness",
                "contract": (
                    "each successful reclaim increments attempt_count and "
                    "(shard_id, attempt_number) remains unique"
                ),
                "attempt_numbers_by_shard_index": attempt_numbers,
                "attempt_statuses_by_shard_index": attempt_statuses,
                "unique_attempt_pairs": unique_attempt_pairs,
                "evidence": (
                    "tests/test_control_scheduling_contracts.py::"
                    "test_scheduling_contract_is_driven_by_real_service_calls"
                ),
            },
            {
                "id": "server_and_attempt_fencing",
                "contract": (
                    "updates from a different server or stale attempt are "
                    "rejected before state mutation"
                ),
                "wrong_server_status": wrong_server.status_code,
                "stale_attempt_status": stale_attempt.status_code,
                "old_terminal_attempt_status": (
                    fenced_after_terminal.status_code
                ),
                "evidence": (
                    "tests/test_control_scheduling_contracts.py::"
                    "test_scheduling_contract_is_driven_by_real_service_calls"
                ),
            },
            {
                "id": "work_shard_lease_lifecycle",
                "contract": (
                    "busy heartbeat renews a live lease; expiry makes the "
                    "shard stale and reclaim creates a new attempt"
                ),
                "heartbeat_extended_lease": shard_lease_extended,
                "expired_status": "stale",
                "reclaimed_status": stale_claim["status"],
                "reclaimed_attempt_count": stale_claim["attempt_count"],
                "reclaimed_server_id": stale_claim[
                    "assigned_server_id"
                ],
                "evidence": (
                    "tests/test_control_scheduling_contracts.py::"
                    "test_scheduling_contract_is_driven_by_real_service_calls"
                ),
            },
            {
                "id": "terminal_monotonicity_and_replay",
                "contract": (
                    "a current terminal update is idempotent and later "
                    "nonterminal replay cannot regress state or counters"
                ),
                "terminal_response": {
                    "status": terminal["status"],
                    "processed_files": terminal["processed_files"],
                },
                "same_terminal_replay": {
                    "status": repeated_terminal["status"],
                    "processed_files": repeated_terminal[
                        "processed_files"
                    ],
                },
                "late_nonterminal_replay": {
                    "status": regressive["status"],
                    "processed_files": regressive["processed_files"],
                },
                "persisted_state": current_state,
                "evidence": (
                    "tests/test_control_scheduling_contracts.py::"
                    "test_scheduling_contract_is_driven_by_real_service_calls"
                ),
            },
            {
                "id": "scan_unit_claim_lease_and_fencing",
                "contract": (
                    "scan units use id ordering, stale-first recovery, lease "
                    "renewal, attempt increments, and server/attempt fencing"
                ),
                "first_claim": {
                    "from_status": "pending",
                    "attempt_count": scan_first["attempt_count"],
                    "server_id": scan_first["assigned_server_id"],
                },
                "heartbeat_extended_lease": scan_lease_extended,
                "reclaim": {
                    "same_unit": scan_reclaim["id"] == scan_first["id"],
                    "from_status": "stale",
                    "attempt_count": scan_reclaim["attempt_count"],
                    "server_id": scan_reclaim["assigned_server_id"],
                },
                "wrong_server_status": scan_wrong_server.status_code,
                "stale_attempt_status": scan_stale_attempt.status_code,
                "old_terminal_attempt_status": scan_old_attempt.status_code,
                "evidence": (
                    "tests/test_control_scheduling_contracts.py::"
                    "test_scheduling_contract_is_driven_by_real_service_calls"
                ),
            },
            {
                "id": "scan_unit_terminal_replay",
                "contract": (
                    "same success/failure terminal replay is idempotent; "
                    "opposite terminal replay is rejected"
                ),
                "success_status": scan_success["status"],
                "success_replay_status": scan_success_replay["status"],
                "late_failure_status": scan_late_fail.status_code,
                "failure_status": scan_failed["status"],
                "failure_replay_status": scan_failed_replay["status"],
                "late_completion_status": scan_late_complete.status_code,
                "evidence": (
                    "tests/test_control_scheduling_contracts.py::"
                    "test_scheduling_contract_is_driven_by_real_service_calls"
                ),
            },
            {
                "id": "stop_state_results",
                "contract": (
                    "stop immediately closes reclaimable work, preserves "
                    "running work until its attempt stops or lease expires, "
                    "and prevents late updates from regressing terminal state"
                ),
                "shards": {
                    "before": before_shard_stop,
                    "after_request_stop": after_shard_stop,
                    "request_job_status": stop_shard_response["status"],
                    "running_terminal_status": stopped_running_shard[
                        "status"
                    ],
                    "final_job_status": stop_shard_summary["status"],
                    "late_current_attempt_status": (
                        stop_shard_late_current["status"]
                    ),
                    "old_attempt_http_status": (
                        stop_shard_old_attempt.status_code
                    ),
                },
                "scan_units": {
                    "before": before_scan_stop,
                    "after_request_stop": after_first_scan_stop,
                    "request_job_status": stop_scan_first["status"],
                    "after_running_lease_expiry": after_scan_expiry,
                    "window_one_job_status": (
                        stop_scan_window_one["status"]
                    ),
                    "after_second_stop_sweep": after_second_scan_stop,
                    "window_two_job_status": (
                        stop_scan_window_two["status"]
                    ),
                },
                "evidence": (
                    "tests/test_control_scheduling_contracts.py::"
                    "test_scheduling_contract_is_driven_by_real_service_calls"
                ),
            },
            {
                "id": "recovery_finalization_bound",
                "contract": (
                    "after stop, current behavior reaches a terminal job "
                    "within at most two lease/recovery sweeps"
                ),
                "work_shard_windows_to_terminal": 1,
                "scan_unit_windows_to_terminal": 2,
                "terminal_status": "stopped",
                "timing_note": (
                    "deterministic controlled-expiry contract; production "
                    "wall-clock validation remains an external gate"
                ),
                "evidence": (
                    "tests/test_control_scheduling_contracts.py::"
                    "test_scheduling_contract_is_driven_by_real_service_calls"
                ),
            },
        ],
        "postgresql_concurrency_validation": {
            "status": "external_required",
            "executed_by_fixture_builder": False,
            "operator": "validation_operator",
            "required_command": (
                "python3 tools/pg_claim_stress.py --database-url "
                "$OCR_PLATFORM_TEST_POSTGRES_URL"
            ),
            "supporting_non_runtime_checks": [
                (
                    "tests/test_control_recovery.py::"
                    "test_claimable_shard_select_uses_postgresql_skip_locked"
                ),
                (
                    "tests/test_control_recovery.py::"
                    "test_claimable_scan_unit_select_uses_postgresql_"
                    "skip_locked_with_limit"
                ),
                "tests/test_pg_claim_stress_tool.py",
            ],
            "disclaimer": (
                "SQLite service scenarios and PostgreSQL SQL compilation do "
                "not prove concurrent SKIP LOCKED behavior"
            ),
        },
    }
    validate_scheduling_contract(contract)
    return contract


def validate_scheduling_contract(contract: dict[str, Any]) -> None:
    """Reject incomplete or internally inconsistent scheduling baselines."""

    if contract.get("schema_version") != 2:
        raise ValueError("scheduling contract schema_version must be 2")
    invariants = {
        item.get("id"): item
        for item in contract.get("invariants", [])
        if isinstance(item, dict)
    }
    required_ids = {
        "claim_ordering",
        "attempt_number_increment_and_uniqueness",
        "server_and_attempt_fencing",
        "work_shard_lease_lifecycle",
        "terminal_monotonicity_and_replay",
        "scan_unit_claim_lease_and_fencing",
        "scan_unit_terminal_replay",
        "stop_state_results",
        "recovery_finalization_bound",
    }
    if set(invariants) != required_ids:
        raise ValueError(
            "scheduling invariant set mismatch: "
            f"{sorted(set(invariants) ^ required_ids)}"
        )
    expected_claims = [
        {"shard_index": 1, "from_status": "pending", "attempt_count": 1},
        {"shard_index": 1, "from_status": "retrying", "attempt_count": 2},
        {"shard_index": 2, "from_status": "pending", "attempt_count": 1},
        {"shard_index": 2, "from_status": "stale", "attempt_count": 2},
        {"shard_index": 3, "from_status": "pending", "attempt_count": 1},
    ]
    if invariants["claim_ordering"].get(
        "observed_claim_sequence"
    ) != expected_claims:
        raise ValueError("work shard claim ordering changed")
    attempts = invariants["attempt_number_increment_and_uniqueness"]
    if attempts.get("attempt_numbers_by_shard_index") != {
        "1": [1, 2],
        "2": [1, 2],
        "3": [1],
    } or attempts.get("unique_attempt_pairs") is not True:
        raise ValueError("work shard attempt numbering changed")
    fencing = invariants["server_and_attempt_fencing"]
    if {
        fencing.get("wrong_server_status"),
        fencing.get("stale_attempt_status"),
        fencing.get("old_terminal_attempt_status"),
    } != {409}:
        raise ValueError("work shard fencing changed")
    lease = invariants["work_shard_lease_lifecycle"]
    if (
        lease.get("heartbeat_extended_lease") is not True
        or lease.get("expired_status") != "stale"
        or lease.get("reclaimed_status") != "running"
        or lease.get("reclaimed_attempt_count") != 2
        or lease.get("reclaimed_server_id") != "worker-b"
    ):
        raise ValueError("work shard lease lifecycle changed")
    terminal = invariants["terminal_monotonicity_and_replay"]
    terminal_response = {"status": "succeeded", "processed_files": 1}
    if any(
        terminal.get(field) != terminal_response
        for field in (
            "terminal_response",
            "same_terminal_replay",
            "late_nonterminal_replay",
        )
    ):
        raise ValueError("work shard terminal replay changed")
    if terminal.get("persisted_state") != {
        "status": "succeeded",
        "attempt_count": 2,
        "assigned_server_id": "worker-b",
        "processed_files": 1,
    }:
        raise ValueError("work shard terminal persistence changed")
    scan = invariants["scan_unit_claim_lease_and_fencing"]
    if (
        scan.get("first_claim")
        != {
            "from_status": "pending",
            "attempt_count": 1,
            "server_id": "worker-a",
        }
        or scan.get("heartbeat_extended_lease") is not True
        or scan.get("reclaim")
        != {
            "same_unit": True,
            "from_status": "stale",
            "attempt_count": 2,
            "server_id": "worker-b",
        }
        or {
            scan.get("wrong_server_status"),
            scan.get("stale_attempt_status"),
            scan.get("old_terminal_attempt_status"),
        }
        != {409}
    ):
        raise ValueError("scan unit claim, lease, or fencing changed")
    scan_terminal = invariants["scan_unit_terminal_replay"]
    if scan_terminal != {
        **{
            "id": "scan_unit_terminal_replay",
            "contract": scan_terminal.get("contract"),
            "evidence": scan_terminal.get("evidence"),
        },
        "success_status": "succeeded",
        "success_replay_status": "succeeded",
        "late_failure_status": 409,
        "failure_status": "failed",
        "failure_replay_status": "failed",
        "late_completion_status": 409,
    }:
        raise ValueError("scan unit terminal replay changed")
    stop = invariants["stop_state_results"]
    if stop.get("shards") != {
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
    }:
        raise ValueError("work shard stop results changed")
    if stop.get("scan_units") != {
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
    }:
        raise ValueError("scan unit stop results changed")
    recovery = invariants["recovery_finalization_bound"]
    if (
        recovery.get("work_shard_windows_to_terminal") != 1
        or recovery.get("scan_unit_windows_to_terminal") != 2
        or recovery.get("terminal_status") != "stopped"
    ):
        raise ValueError("recovery finalization bound changed")
    postgres = contract.get("postgresql_concurrency_validation", {})
    if (
        postgres.get("status") != "external_required"
        or postgres.get("executed_by_fixture_builder") is not False
        or postgres.get("operator") != "validation_operator"
        or "test_postgres_migration_bridge.py"
        in " ".join(postgres.get("supporting_non_runtime_checks", []))
    ):
        raise ValueError("PostgreSQL concurrency gate is misrepresented")


def _column_default(default: Any) -> Any:
    if default is None:
        return None
    argument = default.arg
    if callable(argument):
        return {
            "kind": "callable",
            "value": (
                f"{argument.__module__}."
                f"{getattr(argument, '__qualname__', argument.__name__)}"
            ),
        }
    return {"kind": "scalar", "value": str(argument)}


def _server_default(default: Any) -> str | None:
    if default is None:
        return None
    argument = default.arg
    return str(getattr(argument, "text", argument))


def _type_contract(column_type: Any) -> dict[str, Any]:
    options: dict[str, Any] = {}
    for name in ("length", "precision", "scale", "timezone"):
        if hasattr(column_type, name):
            value = getattr(column_type, name)
            if value is not None:
                options[name] = value
    return {
        "rendered": str(column_type),
        "class": (
            f"{column_type.__class__.__module__}."
            f"{column_type.__class__.__qualname__}"
        ),
        "options": options,
    }


def build_database_contract() -> dict[str, Any]:
    from ocr_platform.control.models import Base

    tables: list[dict[str, Any]] = []
    for table in sorted(Base.metadata.tables.values(), key=lambda item: item.name):
        columns: list[dict[str, Any]] = []
        for column in table.columns:
            columns.append(
                {
                    "name": column.name,
                    "type": _type_contract(column.type),
                    "nullable": bool(column.nullable),
                    "primary_key": bool(column.primary_key),
                    "autoincrement": column.autoincrement,
                    "default": _column_default(column.default),
                    "server_default": _server_default(column.server_default),
                    "foreign_keys": sorted(
                        (
                            {
                                "target": foreign_key.target_fullname,
                                "ondelete": foreign_key.ondelete,
                                "onupdate": foreign_key.onupdate,
                            }
                            for foreign_key in column.foreign_keys
                        ),
                        key=lambda item: item["target"],
                    ),
                }
            )
        indexes = sorted(
            (
                {
                    "name": index.name,
                    "unique": bool(index.unique),
                    "expressions": [
                        str(expression) for expression in index.expressions
                    ],
                }
                for index in table.indexes
            ),
            key=lambda item: item["name"] or "",
        )
        checks = sorted(
            (
                {
                    "name": constraint.name,
                    "sqltext": str(constraint.sqltext),
                }
                for constraint in table.constraints
                if constraint.__class__.__name__ == "CheckConstraint"
            ),
            key=lambda item: (item["name"] or "", item["sqltext"]),
        )
        foreign_keys = sorted(
            (
                {
                    "columns": [
                        element.parent.name for element in constraint.elements
                    ],
                    "referred_table": constraint.referred_table.name,
                    "referred_columns": [
                        element.column.name for element in constraint.elements
                    ],
                    "ondelete": constraint.ondelete,
                    "onupdate": constraint.onupdate,
                }
                for constraint in table.foreign_key_constraints
            ),
            key=lambda item: (
                item["referred_table"],
                tuple(item["columns"]),
                tuple(item["referred_columns"]),
            ),
        )
        tables.append(
            {
                "name": table.name,
                "columns": columns,
                "primary_key": [
                    column.name for column in table.primary_key.columns
                ],
                "foreign_keys": foreign_keys,
                "indexes": indexes,
                "checks": checks,
            }
        )

    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        with engine.connect() as connection:
            effective_indexes: dict[str, list[dict[str, Any]]] = {}
            for table_name in sorted(Base.metadata.tables):
                index_rows = connection.exec_driver_sql(
                    f'PRAGMA index_list("{table_name}")'
                ).mappings()
                table_indexes: list[dict[str, Any]] = []
                for index_row in index_rows:
                    index_name = str(index_row["name"])
                    column_rows = connection.exec_driver_sql(
                        f'PRAGMA index_info("{index_name}")'
                    ).mappings()
                    table_indexes.append(
                        {
                            "name": index_name,
                            "column_names": [
                                str(column["name"])
                                for column in sorted(
                                    column_rows,
                                    key=lambda item: int(item["seqno"]),
                                )
                            ],
                            "unique": bool(index_row["unique"]),
                            "origin": str(index_row["origin"]),
                            "partial": bool(index_row["partial"]),
                        }
                    )
                effective_indexes[table_name] = sorted(
                    table_indexes,
                    key=lambda item: item["name"],
                )
    finally:
        engine.dispose()

    migration_fixture = read_json(MIGRATION_FIXTURE)
    return {
        "schema_version": 1,
        "orm_metadata": {
            "table_count": len(tables),
            "column_count": sum(len(table["columns"]) for table in tables),
            "index_count": sum(len(table["indexes"]) for table in tables),
            "foreign_key_count": sum(
                len(table["foreign_keys"]) for table in tables
            ),
            "tables": tables,
        },
        "sqlite_effective_indexes": {
            "index_count": sum(
                len(indexes) for indexes in effective_indexes.values()
            ),
            "tables": effective_indexes,
        },
        # Do not duplicate migration versions or digests here. The existing
        # fixed-byte fixture remains the single migration-history truth source.
        "migration_history_reference": {
            "path": str(MIGRATION_FIXTURE.relative_to(ROOT)),
            "sha256": hashlib.sha256(MIGRATION_FIXTURE.read_bytes()).hexdigest(),
            "count": len(migration_fixture["migrations"]),
            "latest": list(migration_fixture["migrations"])[-1],
        },
    }


def _literal_strings(node: ast.AST | None) -> list[str]:
    if node is None:
        return []
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.IfExp):
        return sorted(
            set(_literal_strings(node.body))
            | set(_literal_strings(node.orelse))
        )
    if isinstance(node, ast.BoolOp):
        return sorted(
            {
                value
                for child in node.values
                for value in _literal_strings(child)
            }
        )
    if isinstance(node, (ast.List, ast.Set, ast.Tuple)):
        return sorted(
            {
                value
                for child in node.elts
                for value in _literal_strings(child)
            }
        )
    return []


def _record_value_evidence(
    evidence: dict[str, list[dict[str, str]]],
    values: list[str],
    *,
    kind: str,
    source: str,
    symbol: str,
) -> None:
    for value in values:
        item = {"kind": kind, "source": source, "symbol": symbol}
        if item not in evidence.setdefault(value, []):
            evidence[value].append(item)


def _class_field_source(
    path: Path,
    class_name: str,
    field_name: str,
) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == field_name
            ):
                return f"{path.relative_to(ROOT).as_posix()}:{statement.lineno}"
    raise RuntimeError(f"cannot find {class_name}.{field_name} in {path}")


def _class_member_source(
    path: Path,
    class_name: str,
    member_name: str,
) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for statement in node.body:
            if (
                isinstance(statement, ast.Assign)
                and any(
                    isinstance(target, ast.Name)
                    and target.id == member_name
                    for target in statement.targets
                )
            ):
                return f"{path.relative_to(ROOT).as_posix()}:{statement.lineno}"
    raise RuntimeError(f"cannot find {class_name}.{member_name} in {path}")


def _constant_source(path: Path, symbol: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in tree.body:
        if (
            isinstance(node, (ast.Assign, ast.AnnAssign))
            and (
                (
                    isinstance(node, ast.Assign)
                    and any(
                        isinstance(target, ast.Name)
                        and target.id == symbol
                        for target in node.targets
                    )
                )
                or (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == symbol
                )
            )
        ):
            return f"{path.relative_to(ROOT).as_posix()}:{node.lineno}"
    raise RuntimeError(f"cannot find {symbol} in {path}")


def _status_transition_evidence(
    paths: list[Path],
    *,
    constructor: str | None = None,
    sql_model: str | None = None,
    instance_field: tuple[str, str] | None = None,
) -> dict[str, list[dict[str, str]]]:
    evidence: dict[str, list[dict[str, str]]] = {}
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        relative_path = path.relative_to(ROOT).as_posix()
        for node in ast.walk(tree):
            values: list[str] = []
            symbol = ""
            if isinstance(node, ast.Call):
                terminal_name = _call_terminal_name(node)
                status_keyword = next(
                    (
                        keyword.value
                        for keyword in node.keywords
                        if keyword.arg == "status"
                    ),
                    None,
                )
                if constructor and terminal_name == constructor:
                    values = _literal_strings(status_keyword)
                    symbol = f"{constructor}(status=...)"
                elif (
                    sql_model
                    and terminal_name == "values"
                    and any(
                        isinstance(item, ast.Name)
                        and item.id == sql_model
                        for item in ast.walk(node)
                    )
                ):
                    values = _literal_strings(status_keyword)
                    symbol = f"update({sql_model}).values(status=...)"
            elif (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and instance_field is not None
            ):
                targets = (
                    node.targets
                    if isinstance(node, ast.Assign)
                    else [node.target]
                )
                value_node = node.value
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == instance_field[0]
                        and target.attr == instance_field[1]
                    ):
                        values = _literal_strings(value_node)
                        symbol = ".".join(instance_field)
            if values:
                _record_value_evidence(
                    evidence,
                    values,
                    kind="domain_transition",
                    source=f"{relative_path}:{node.lineno}",
                    symbol=symbol,
                )
    return {
        value: sorted(
            items,
            key=lambda item: (
                item["source"],
                item["kind"],
                item["symbol"],
            ),
        )
        for value, items in sorted(evidence.items())
    }


def _constructor_status_evidence(
    path: Path,
    constructor: str,
) -> dict[str, list[dict[str, str]]]:
    return _status_transition_evidence(
        [path],
        constructor=constructor,
    )


def _attribute_assignment_sources(
    path: Path,
    *,
    target: tuple[str, str],
    value: tuple[str, str],
    expected_count: int,
) -> list[str]:
    """Locate an exact ``target.attr = value.attr`` source relationship."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        if not (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and (node.value.value.id, node.value.attr) == value
        ):
            continue
        if any(
            isinstance(candidate, ast.Attribute)
            and isinstance(candidate.value, ast.Name)
            and (candidate.value.id, candidate.attr) == target
            for candidate in targets
        ):
            lines.append(node.lineno)
    if len(lines) != expected_count:
        raise ValueError(
            f"expected {expected_count} assignments "
            f"{'.'.join(target)} = {'.'.join(value)} in "
            f"{path}, found {len(lines)}"
        )
    relative = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    return [f"{relative}:{line}" for line in sorted(lines)]


def _constructor_keyword_projection_sources(
    path: Path,
    *,
    constructor: str,
    keyword: str,
    value: tuple[str, str],
    expected_count: int,
) -> list[str]:
    """Locate exact constructor keyword projections from an object field."""

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    lines: list[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        function_name = (
            node.func.id
            if isinstance(node.func, ast.Name)
            else node.func.attr
            if isinstance(node.func, ast.Attribute)
            else None
        )
        if function_name != constructor:
            continue
        for item in node.keywords:
            if (
                item.arg == keyword
                and isinstance(item.value, ast.Attribute)
                and isinstance(item.value.value, ast.Name)
                and (item.value.value.id, item.value.attr) == value
            ):
                lines.append(item.value.lineno)
    if len(lines) != expected_count:
        raise ValueError(
            f"expected {expected_count} {constructor} projections "
            f"{keyword}={'.'.join(value)} in {path}, found {len(lines)}"
        )
    relative = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    return [f"{relative}:{line}" for line in sorted(lines)]


def _authority(
    authority_type: str,
    sources: list[str],
    value_evidence: dict[str, list[dict[str, str]]],
) -> dict[str, Any]:
    return {
        "type": authority_type,
        "sources": sorted(sources),
        "authoritative_values": sorted(value_evidence),
        "value_evidence": {
            value: evidence
            for value, evidence in sorted(value_evidence.items())
        },
    }


def validate_status_contract(contract: dict[str, Any]) -> None:
    entities = contract.get("entities", {})
    if len(entities) != 13:
        raise ValueError(
            f"status contract has {len(entities)} surfaces, expected 13"
        )
    for surface, entity in entities.items():
        openness = str(entity.get("openness", ""))
        authority = entity.get("authority", {})
        if openness.startswith("closed_"):
            values = entity.get("values")
            authoritative_values = authority.get("authoritative_values")
            if values != sorted(set(values or [])):
                raise ValueError(
                    f"{surface} closed values must be sorted and unique"
                )
            if values != authoritative_values:
                raise ValueError(
                    f"{surface} closed values differ from authority"
                )
            value_evidence = authority.get("value_evidence", {})
            if set(value_evidence) != set(values):
                raise ValueError(
                    f"{surface} lacks per-value authority evidence"
                )
            if any(not evidence for evidence in value_evidence.values()):
                raise ValueError(
                    f"{surface} contains empty value evidence"
                )
        else:
            if "values" in entity:
                raise ValueError(
                    f"{surface} is open and cannot declare exhaustive values"
                )
            if not entity.get("open_reason"):
                raise ValueError(f"{surface} lacks an open-set reason")

    projection_expectations = {
        "ShardAttempt": {
            "kind": "ast_attribute_assignment",
            "target": "attempt.status",
            "source": "shard.status",
            "expected_count": 1,
            "evidence_kind": "derived_transition",
        },
        "ManifestFreezeReport.status": {
            "kind": "ast_constructor_keyword",
            "constructor": "ManifestFreezeReportResponse",
            "keyword": "status",
            "source": "manifest.status",
            "expected_count": 2,
            "projected_values": ["failed", "ready", "scanning"],
            "evidence_kind": "response_projection",
        },
    }
    for surface, expected in projection_expectations.items():
        authority = entities.get(surface, {}).get("authority", {})
        relationship = authority.get("projection_relationship", {})
        for key, value in expected.items():
            if key == "evidence_kind":
                continue
            if relationship.get(key) != value:
                raise ValueError(
                    f"{surface} projection relationship {key} changed"
                )
        matches = relationship.get("matches", [])
        if (
            len(matches) != expected["expected_count"]
            or len(set(matches)) != len(matches)
        ):
            raise ValueError(
                f"{surface} projection relationship match count changed"
            )
        authority_sources = set(authority.get("sources", []))
        if not set(matches) <= authority_sources:
            raise ValueError(
                f"{surface} projection matches are absent from sources"
            )
        evidence_kind = expected["evidence_kind"]
        projected_values = relationship.get(
            "projected_values",
            entities[surface]["values"],
        )
        if not set(projected_values) <= set(entities[surface]["values"]):
            raise ValueError(
                f"{surface} projected values exceed the response values"
            )
        for value in projected_values:
            projection_sources = {
                item["source"]
                for item in authority["value_evidence"][value]
                if item["kind"] == evidence_kind
            }
            if projection_sources != set(matches):
                raise ValueError(
                    f"{surface} value {value} projection evidence changed"
                )


def build_status_contract() -> dict[str, Any]:
    """Build status surfaces from independent runtime and source authorities."""

    from ocr_parser.domain.metadata import SUCCESS_STATUSES
    from ocr_platform.control.domains.common import (
        JOB_STATUS_FILTERS,
        PROCESSED_FILE_STATUSES,
    )
    from ocr_platform.control.models import (
        JobFile,
        ModelProfileCertification,
        Server,
    )
    from ocr_platform.control.schemas import (
        ServerHeartbeatRequest,
        WorkShardUpdateRequest,
    )

    common_path = ROOT / "ocr_platform" / "control" / "domains" / "common.py"
    schemas_path = ROOT / "ocr_platform" / "control" / "schemas.py"
    models_path = ROOT / "ocr_platform" / "control" / "models.py"
    manifests_path = (
        ROOT / "ocr_platform" / "control" / "domains" / "manifests" / "core.py"
    )
    workers_path = (
        ROOT / "ocr_platform" / "control" / "domains" / "workers" / "core.py"
    )
    jobs_path = (
        ROOT / "ocr_platform" / "control" / "domains" / "jobs" / "core.py"
    )
    metadata_path = ROOT / "ocr_parser" / "domain" / "metadata.py"

    job_values = sorted(str(value) for value in JOB_STATUS_FILTERS)
    job_source = _constant_source(common_path, "JOB_STATUS_FILTERS")
    job_evidence: dict[str, list[dict[str, str]]] = {}
    _record_value_evidence(
        job_evidence,
        job_values,
        kind="domain_constant",
        source=job_source,
        symbol="JOB_STATUS_FILTERS",
    )

    work_shard_values = sorted(
        str(value)
        for value in get_args(
            WorkShardUpdateRequest.model_fields["status"].annotation
        )
    )
    work_shard_source = _class_field_source(
        schemas_path,
        "WorkShardUpdateRequest",
        "status",
    )
    work_shard_evidence: dict[str, list[dict[str, str]]] = {}
    _record_value_evidence(
        work_shard_evidence,
        work_shard_values,
        kind="pydantic_literal",
        source=work_shard_source,
        symbol="WorkShardUpdateRequest.status",
    )

    attempt_values = sorted(set(work_shard_values) - {"pending"})
    attempt_evidence: dict[str, list[dict[str, str]]] = {}
    _record_value_evidence(
        attempt_evidence,
        attempt_values,
        kind="derived_input",
        source=work_shard_source,
        symbol="WorkShardUpdateRequest.status excluding pre-claim pending",
    )
    attempt_projection_sources = _attribute_assignment_sources(
        manifests_path,
        target=("attempt", "status"),
        value=("shard", "status"),
        expected_count=1,
    )
    for source in attempt_projection_sources:
        _record_value_evidence(
            attempt_evidence,
            attempt_values,
            kind="derived_transition",
            source=source,
            symbol="attempt.status = shard.status",
        )

    scan_evidence = _status_transition_evidence(
        [manifests_path, workers_path],
        constructor="ScanUnit",
        sql_model="ScanUnit",
        instance_field=("unit", "status"),
    )
    manifest_evidence = _status_transition_evidence(
        [manifests_path],
        constructor="Manifest",
        instance_field=("manifest", "status"),
    )
    worker_integrity_evidence = _status_transition_evidence(
        [manifests_path],
        instance_field=("manifest", "worker_integrity_status"),
    )
    freeze_literal_evidence = _constructor_status_evidence(
        manifests_path,
        "ManifestFreezeReportResponse",
    )
    freeze_evidence = copy.deepcopy(manifest_evidence)
    for value, evidence in freeze_literal_evidence.items():
        freeze_evidence.setdefault(value, []).extend(evidence)
    freeze_projection_sources = _constructor_keyword_projection_sources(
        manifests_path,
        constructor="ManifestFreezeReportResponse",
        keyword="status",
        value=("manifest", "status"),
        expected_count=2,
    )
    for value in manifest_evidence:
        for source in freeze_projection_sources:
            freeze_evidence[value].append(
                {
                    "kind": "response_projection",
                    "source": source,
                    "symbol": "status=manifest.status",
                }
            )
        freeze_evidence[value] = sorted(
            freeze_evidence[value],
            key=lambda item: (
                item["source"],
                item["kind"],
                item["symbol"],
            ),
        )

    integrity_known_evidence = _constructor_status_evidence(
        manifests_path,
        "ManifestIntegrityResponse",
    )

    certification_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in ModelProfileCertification.__table__.constraints
        if constraint.__class__.__name__ == "CheckConstraint"
    }

    def database_check_authority(constraint_name: str) -> dict[str, Any]:
        values = sorted(
            re.findall(r"'([^']+)'", certification_checks[constraint_name])
        )
        source = _class_member_source(
            models_path,
            "ModelProfileCertification",
            "__table_args__",
        )
        evidence: dict[str, list[dict[str, str]]] = {}
        _record_value_evidence(
            evidence,
            values,
            kind="database_check",
            source=source,
            symbol=constraint_name,
        )
        return _authority("database_check", [source], evidence)

    scheduling = build_scheduling_contract()
    claim_invariant = next(
        item
        for item in scheduling["invariants"]
        if item["id"] == "claim_ordering"
    )
    attempt_invariant = next(
        item
        for item in scheduling["invariants"]
        if item["id"] == "attempt_number_increment_and_uniqueness"
    )
    terminal_invariant = next(
        item
        for item in scheduling["invariants"]
        if item["id"] == "terminal_monotonicity_and_replay"
    )
    observed_work_shard = sorted(
        {
            item["from_status"]
            for item in claim_invariant["observed_claim_sequence"]
        }
        | {
            terminal_invariant["terminal_response"]["status"],
            *(
                status
                for statuses in attempt_invariant[
                    "attempt_statuses_by_shard_index"
                ].values()
                for status in statuses
            ),
        }
    )
    observed_attempt = sorted(
        {
            status
            for statuses in attempt_invariant[
                "attempt_statuses_by_shard_index"
            ].values()
            for status in statuses
        }
    )

    server_known = {
        str(Server.__table__.c.status.default.arg),
        str(ServerHeartbeatRequest.model_fields["status"].default),
    }
    server_transition_evidence = _status_transition_evidence(
        [workers_path],
        constructor="Server",
        instance_field=("server", "status"),
    )
    server_known.update(server_transition_evidence)
    server_known.update({"busy"})

    job_file_known = {
        str(value) for value in PROCESSED_FILE_STATUSES
    } | {str(JobFile.__table__.c.status.default.arg)}
    job_file_transition_evidence = _status_transition_evidence(
        [jobs_path],
        instance_field=("job_file", "status"),
    )
    job_file_known.update(job_file_transition_evidence)

    attempt_authority = _authority(
        "derived_domain_transition",
        [work_shard_source, *attempt_projection_sources],
        attempt_evidence,
    )
    attempt_authority["projection_relationship"] = {
        "kind": "ast_attribute_assignment",
        "target": "attempt.status",
        "source": "shard.status",
        "matches": attempt_projection_sources,
        "expected_count": 1,
    }
    freeze_authority = _authority(
        "derived_response_projection",
        [
            str(manifests_path.relative_to(ROOT)),
            *freeze_projection_sources,
        ],
        freeze_evidence,
    )
    freeze_authority["projection_relationship"] = {
        "kind": "ast_constructor_keyword",
        "constructor": "ManifestFreezeReportResponse",
        "keyword": "status",
        "source": "manifest.status",
        "matches": freeze_projection_sources,
        "expected_count": 2,
        "projected_values": sorted(manifest_evidence),
    }

    entities: dict[str, Any] = {
        "Job": {
            "storage": "string",
            "openness": "closed_domain_set",
            "values": job_values,
            "authority": _authority(
                "domain_constant",
                [job_source],
                job_evidence,
            ),
            "behavior_observed_values": [],
        },
        "WorkShard": {
            "storage": "string",
            "openness": "closed_wire_enum",
            "values": work_shard_values,
            "authority": _authority(
                "pydantic_literal",
                [work_shard_source],
                work_shard_evidence,
            ),
            "behavior_observed_values": observed_work_shard,
            "behavior_evidence": scheduling["builder"],
        },
        "ShardAttempt": {
            "storage": "string",
            "openness": "closed_derived_set",
            "values": attempt_values,
            "authority": attempt_authority,
            "behavior_observed_values": observed_attempt,
            "behavior_evidence": scheduling["builder"],
        },
        "ScanUnit": {
            "storage": "string",
            "openness": "closed_domain_set",
            "values": sorted(scan_evidence),
            "authority": _authority(
                "ast_domain_transitions",
                [str(path.relative_to(ROOT)) for path in [manifests_path, workers_path]],
                scan_evidence,
            ),
            "behavior_observed_values": [],
        },
        "Server": {
            "storage": "string",
            "openness": "open_external_string",
            "known_values": sorted(server_known),
            "authority": {
                "type": "unconstrained_pydantic_string",
                "sources": [
                    _class_field_source(
                        schemas_path,
                        "ServerHeartbeatRequest",
                        "status",
                    )
                ],
            },
            "open_reason": (
                "ServerHeartbeatRequest.status is a non-empty str supplied by "
                "the Agent; no Literal or database CHECK limits its values."
            ),
        },
        "Manifest": {
            "storage": "string",
            "openness": "closed_domain_set",
            "values": sorted(manifest_evidence),
            "authority": _authority(
                "ast_domain_transitions",
                [str(manifests_path.relative_to(ROOT))],
                manifest_evidence,
            ),
            "behavior_observed_values": [],
        },
        "Manifest.worker_integrity_status": {
            "storage": "nullable_string",
            "openness": "closed_domain_set",
            "values": sorted(worker_integrity_evidence),
            "authority": _authority(
                "ast_domain_transitions",
                [str(manifests_path.relative_to(ROOT))],
                worker_integrity_evidence,
            ),
            "behavior_observed_values": [],
        },
        "ManifestFreezeReport.status": {
            "storage": "response_string",
            "openness": "closed_response_set",
            "values": sorted(freeze_evidence),
            "authority": freeze_authority,
            "behavior_observed_values": [],
        },
        "ManifestIntegrityResponse.status": {
            "storage": "response_string",
            "openness": "open_external_string",
            "known_values": sorted(integrity_known_evidence),
            "authority": {
                "type": "worker_report_string",
                "sources": [
                    _class_field_source(
                        schemas_path,
                        "ManifestIntegrityResponse",
                        "status",
                    ),
                    "ocr_platform/control/domains/manifests/core.py:888",
                    "ocr_platform/control/domains/manifests/core.py:893",
                ],
            },
            "known_value_evidence": integrity_known_evidence,
            "open_reason": (
                "ManifestIntegrityWorkerCompleteRequest accepts a worker "
                "ManifestIntegrityResponse whose status field is str; the "
                "payload is persisted and can be projected back unchanged."
            ),
        },
        "JobFile": {
            "storage": "string",
            "openness": "open_event_string",
            "known_values": sorted(job_file_known),
            "authority": {
                "type": "event_payload_string",
                "sources": [
                    _class_field_source(
                        schemas_path,
                        "JobEventRequest",
                        "payload",
                    ),
                    "ocr_platform/control/domains/jobs/core.py:1242",
                ],
            },
            "open_reason": (
                "JobFile.status is assigned from JobEventRequest.payload, "
                "whose status member has no schema enum."
            ),
        },
        "JobEvent": {
            "storage": "nullable_string",
            "openness": "open_event_string",
            "known_values": sorted(str(value) for value in SUCCESS_STATUSES),
            "authority": {
                "type": "event_payload_string",
                "sources": [
                    _class_field_source(
                        schemas_path,
                        "JobEventRequest",
                        "payload",
                    ),
                    "ocr_platform/control/domains/jobs/core.py:1559",
                    _constant_source(metadata_path, "SUCCESS_STATUSES"),
                ],
            },
            "open_reason": (
                "JobEvent.status is copied from an arbitrary event payload; "
                "known parser statuses are examples, not an exhaustive set."
            ),
        },
        "ModelProfileCertification.enforcement": {
            "storage": "string",
            "openness": "closed_database_check",
            "authority": database_check_authority(
                "ck_model_profile_certifications_enforcement"
            ),
            "behavior_observed_values": [],
        },
        "ModelProfileCertification.status": {
            "storage": "string",
            "openness": "closed_database_check",
            "authority": database_check_authority(
                "ck_model_profile_certifications_status"
            ),
            "behavior_observed_values": [],
        },
    }
    for entity in entities.values():
        if str(entity["openness"]).startswith("closed_"):
            entity.setdefault(
                "values",
                list(entity["authority"]["authoritative_values"]),
            )
    contract = {
        "schema_version": 2,
        "surface_count": len(entities),
        "entities": entities,
    }
    validate_status_contract(contract)
    return contract


def build_all_contracts() -> dict[str, dict[str, Any]]:
    with _temporary_environment(remove=CONTROL_CONTRACT_ENV_VARS):
        return {
            "openapi": build_openapi_contract(),
            "http": build_http_behavior_contract(),
            "http_operation_matrix": build_http_operation_matrix(),
            "database": build_database_contract(),
            "status": build_status_contract(),
            "scheduling": build_scheduling_contract(),
        }


def render_contract(name: str, payload: dict[str, Any]) -> str:
    return canonical_json(payload, compact=name == "openapi")


def contract_file_matches(
    name: str,
    observed: dict[str, Any],
    fixture_path: Path,
) -> bool:
    """Return whether a fixture is the exact rendered observed contract."""

    return (
        fixture_path.exists()
        and fixture_path.read_text(encoding="utf-8")
        == render_contract(name, observed)
    )


def refresh() -> None:
    CONTRACT_ROOT.mkdir(parents=True, exist_ok=True)
    for name, payload in build_all_contracts().items():
        path = CONTRACT_FILES[name]
        path.write_text(render_contract(name, payload), encoding="utf-8")
        print(f"wrote {path.relative_to(ROOT)}")


def check() -> int:
    failures: list[str] = []
    for name, payload in build_all_contracts().items():
        path = CONTRACT_FILES[name]
        if not path.exists():
            failures.append(f"missing {path.relative_to(ROOT)}")
        elif not contract_file_matches(name, payload, path):
            failures.append(
                f"{path.relative_to(ROOT)} is stale; run "
                "python tools/control_contracts.py refresh"
            )
    if failures:
        print("\n".join(failures), file=sys.stderr)
        return 1
    print("Control contracts match checked-in baselines.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser(
        "refresh",
        help="Rewrite the checked-in fixtures for explicit review.",
    )
    subparsers.add_parser(
        "check",
        help="Compare generated contracts with the checked-in fixtures.",
    )
    args = parser.parse_args(argv)
    if args.command == "refresh":
        refresh()
        return 0
    return check()


if __name__ == "__main__":
    raise SystemExit(main())
