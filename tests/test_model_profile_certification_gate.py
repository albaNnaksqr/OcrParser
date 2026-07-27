from __future__ import annotations

import ast
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import ocr_platform.control.certification_gate as certification_gate
from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.models import (
    ModelProfile,
    ModelProfileCertification,
    Server,
)
from ocr_platform.control.domains.model_profiles.policy import (
    MODEL_PROFILE_CERTIFICATION_MISMATCH,
    MODEL_PROFILE_CERTIFICATION_MISSING,
    MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED,
)


PROFILE_ID = "certification-gate-profile"
PARSER_REVISION = "47e1c0399db97f4ec48715548b8c937bc77c20ba"
SHA_A = f"sha256:{'a' * 64}"
SHA_B = f"sha256:{'b' * 64}"
SHA_C = f"sha256:{'c' * 64}"


def make_client_with_session(tmp_path):
    session_factory, engine = create_session_factory(
        f"sqlite:///{tmp_path / 'control.db'}"
    )
    init_db(engine)
    return (
        TestClient(create_app(session_factory=session_factory)),
        session_factory,
    )


def certified_values(**updates):
    values = {
        "enforcement": "certified",
        "status": "certified",
        "parser_revision": PARSER_REVISION,
        "model_revision": "model-r1",
        "runtime_digest": SHA_A,
        "fixture_set_digest": SHA_B,
        "evidence_digest": SHA_C,
        "risk_acceptance_json": "{}",
    }
    values.update(updates)
    return values


def exact_engine_provenance(**profile_updates):
    profile = {
        "model_revision": "model-r1",
        "runtime_digest": SHA_A,
    }
    profile.update(profile_updates)
    return {
        "source_revision": PARSER_REVISION,
        "dirty": False,
        "profiles": {PROFILE_ID: profile},
    }


def add_profile(
    session_factory,
    *,
    certification: dict[str, object] | None,
    ip: str | None = None,
    port: int | None = None,
    api_key: str | None = None,
):
    with session_factory() as session:
        profile = ModelProfile(
            id=PROFILE_ID,
            label="Certification gate profile",
            engine="dotsocr",
            ip=ip,
            port=port,
            model_name="DotsOCR",
            extra_args_json="{}",
            api_key=api_key,
            requires_api_key=bool(api_key),
        )
        session.add(profile)
        if certification is not None:
            session.add(
                ModelProfileCertification(
                    profile_id=PROFILE_ID,
                    **certification,
                )
            )
        session.commit()


def register_worker(
    client: TestClient,
    server_id: str,
    *,
    engine_provenance: dict[str, object] | None,
    can_access: bool = True,
):
    capabilities: dict[str, object] = {
        "shared_paths": [
            {
                "path": "/shared",
                "exists": can_access,
                "is_dir": can_access,
                "readable": can_access,
                "writable": can_access,
            }
        ],
    }
    if engine_provenance is not None:
        capabilities["engine_provenance"] = engine_provenance
    response = client.post(
        "/api/servers/register",
        json={
            "id": server_id,
            "name": "Certification Worker",
            "host": "worker.example",
            "capabilities": capabilities,
        },
    )
    assert response.status_code == 200, response.text


def job_payload(
    *,
    assigned_server_id: str | None = "worker-a",
    allowed_server_ids: list[str] | None = None,
    input_mode: str = "directory",
) -> dict[str, object]:
    return {
        "input_dir": "/shared/input",
        "output_dir": "/shared/output",
        "engine": "dotsocr",
        "model_profile_id": PROFILE_ID,
        "assigned_server_id": assigned_server_id,
        "allowed_server_ids": allowed_server_ids or [],
        "input_mode": input_mode,
    }


def assert_preflight_and_create_code(
    client: TestClient,
    payload: dict[str, object],
    expected_code: str,
) -> None:
    preflight = client.post("/api/jobs/preflight", json=payload)
    create = client.post("/api/jobs", json=payload)

    assert preflight.status_code == 200
    certification_issues = [
        issue
        for issue in preflight.json()["issues"]
        if issue["code"].startswith("model_profile_")
        and issue["code"] != "model_profile_saved_api_key"
    ]
    assert [issue["code"] for issue in certification_issues] == [
        expected_code
    ]
    assert certification_issues[0]["severity"] == "error"
    assert create.status_code == 400
    assert create.json()["detail"]["code"] == expected_code
    assert create.json()["detail"]["details"] == (
        certification_issues[0]["details"]
    )


def test_certification_gate_has_no_worker_domain_dependency():
    source_path = Path(certification_gate.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules = {
        node.module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    imported_modules.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )

    assert not any(
        module.endswith("domains.workers")
        or module.endswith("domains.workers.core")
        for module in imported_modules
    )
    assert "domains.workers" not in source
    assert "workers.core" not in source


@pytest.mark.parametrize(
    "missing_field",
    [
        "parser_revision",
        "model_revision",
        "runtime_digest",
        "fixture_set_digest",
        "evidence_digest",
    ],
)
def test_certified_missing_profile_fields_match_preflight_and_create(
    tmp_path,
    monkeypatch,
    missing_field,
):
    client, session_factory = make_client_with_session(tmp_path)
    values = certified_values()
    values[missing_field] = None
    add_profile(session_factory, certification=values)
    register_worker(
        client,
        "worker-a",
        engine_provenance=exact_engine_provenance(),
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": PARSER_REVISION, "dirty": False},
    )

    assert_preflight_and_create_code(
        client,
        job_payload(),
        MODEL_PROFILE_CERTIFICATION_MISSING,
    )


@pytest.mark.parametrize(
    ("build", "expected_code"),
    [
        ({}, MODEL_PROFILE_CERTIFICATION_MISSING),
        (
            {"source_revision": PARSER_REVISION},
            MODEL_PROFILE_CERTIFICATION_MISSING,
        ),
        (
            {"source_revision": "different-parser", "dirty": False},
            MODEL_PROFILE_CERTIFICATION_MISMATCH,
        ),
        (
            {"source_revision": PARSER_REVISION, "dirty": True},
            MODEL_PROFILE_CERTIFICATION_MISMATCH,
        ),
    ],
)
def test_control_build_failures_match_preflight_and_create(
    tmp_path,
    monkeypatch,
    build,
    expected_code,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(),
    )
    register_worker(
        client,
        "worker-a",
        engine_provenance=exact_engine_provenance(),
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: build,
    )

    assert_preflight_and_create_code(
        client,
        job_payload(),
        expected_code,
    )


@pytest.mark.parametrize(
    ("engine_provenance", "expected_code"),
    [
        (None, MODEL_PROFILE_CERTIFICATION_MISSING),
        (
            {
                "source_revision": PARSER_REVISION,
                "dirty": False,
                "profiles": {},
            },
            MODEL_PROFILE_CERTIFICATION_MISSING,
        ),
        (
            {
                **exact_engine_provenance(),
                "source_revision": "different-parser",
            },
            MODEL_PROFILE_CERTIFICATION_MISMATCH,
        ),
        (
            {
                **exact_engine_provenance(),
                "dirty": True,
            },
            MODEL_PROFILE_CERTIFICATION_MISMATCH,
        ),
        (
            exact_engine_provenance(model_revision="different-model"),
            MODEL_PROFILE_CERTIFICATION_MISMATCH,
        ),
        (
            exact_engine_provenance(runtime_digest=SHA_B),
            MODEL_PROFILE_CERTIFICATION_MISMATCH,
        ),
    ],
)
def test_agent_failures_match_preflight_and_create(
    tmp_path,
    monkeypatch,
    engine_provenance,
    expected_code,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(),
    )
    register_worker(
        client,
        "worker-a",
        engine_provenance=engine_provenance,
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": PARSER_REVISION, "dirty": False},
    )

    assert_preflight_and_create_code(
        client,
        job_payload(),
        expected_code,
    )


@pytest.mark.parametrize(
    ("field", "expected", "actual"),
    [
        ("model_digest", SHA_B, SHA_C),
        ("runtime_revision", "runtime-r1", "runtime-r2"),
        ("layout_revision", "layout-r1", "layout-r2"),
        ("layout_digest", SHA_B, SHA_C),
    ],
)
def test_optional_agent_mismatches_match_preflight_and_create(
    tmp_path,
    monkeypatch,
    field,
    expected,
    actual,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(**{field: expected}),
    )
    register_worker(
        client,
        "worker-a",
        engine_provenance=exact_engine_provenance(**{field: actual}),
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": PARSER_REVISION, "dirty": False},
    )

    assert_preflight_and_create_code(
        client,
        job_payload(),
        MODEL_PROFILE_CERTIFICATION_MISMATCH,
    )


@pytest.mark.parametrize(
    ("certification", "expected_code"),
    [
        (
            {
                "enforcement": "verified",
                "status": "verified",
                "risk_acceptance_json": "{}",
            },
            MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED,
        ),
        (
            {
                "enforcement": "verified",
                "status": "verified",
                "risk_acceptance_json": json.dumps(
                    {
                        "accepted_by": "",
                        "accepted_at": "2026-07-28T09:00:00+08:00",
                        "reason": "accepted",
                    }
                ),
            },
            MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED,
        ),
        (
            {
                "enforcement": "verified",
                "status": "verified",
                "risk_acceptance_json": json.dumps(
                    {
                        "accepted_by": "operator",
                        "accepted_at": "2026-07-28T09:00:00",
                        "reason": "accepted",
                    }
                ),
            },
            MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED,
        ),
        (
            {
                "enforcement": "verified",
                "status": "verified",
                "risk_acceptance_json": json.dumps(
                    {
                        "accepted_by": "operator",
                        "accepted_at": "2026-07-28T09:00:00+08:00",
                        "reason": "",
                    }
                ),
            },
            MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED,
        ),
        (
            {
                "enforcement": "verified",
                "status": "verified",
                "risk_acceptance_json": "{",
            },
            MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED,
        ),
    ],
)
def test_verified_risk_failures_match_preflight_and_create(
    tmp_path,
    monkeypatch,
    certification,
    expected_code,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(session_factory, certification=certification)
    register_worker(
        client,
        "worker-a",
        engine_provenance=None,
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {},
    )

    assert_preflight_and_create_code(
        client,
        job_payload(),
        expected_code,
    )


@pytest.mark.parametrize(
    ("updates", "unsafe_value"),
    [
        ({"model_revision": None}, None),
        (
            {"parser_revision": "http://private.internal:31180/model"},
            "private.internal",
        ),
    ],
)
def test_verified_enforcement_certified_historical_profile_damage_is_missing(
    tmp_path,
    monkeypatch,
    updates,
    unsafe_value,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(
            enforcement="verified",
            **updates,
        ),
    )
    register_worker(
        client,
        "worker-a",
        engine_provenance=None,
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {},
    )
    payload = job_payload()

    preflight = client.post("/api/jobs/preflight", json=payload)
    create = client.post("/api/jobs", json=payload)

    assert preflight.status_code == 200
    matching = [
        issue
        for issue in preflight.json()["issues"]
        if issue["code"] == MODEL_PROFILE_CERTIFICATION_MISSING
    ]
    assert len(matching) == 1
    assert create.status_code == 400
    assert create.json()["detail"]["code"] == (
        MODEL_PROFILE_CERTIFICATION_MISSING
    )
    if unsafe_value is not None:
        assert unsafe_value not in preflight.text
        assert unsafe_value not in create.text


@pytest.mark.parametrize(
    "certification",
    [
        None,
        {
            "enforcement": "off",
            "status": "contract_only",
            "risk_acceptance_json": "{}",
        },
        {
            "enforcement": "verified",
            "status": "verified",
            "risk_acceptance_json": json.dumps(
                {
                    "accepted_by": "release-operator",
                    "accepted_at": "2026-07-28T09:00:00+08:00",
                    "reason": "approved-for-rollout",
                }
            ),
        },
        {
            **certified_values(),
            "enforcement": "verified",
        },
    ],
)
def test_legacy_off_and_valid_verified_profiles_allow_old_agents(
    tmp_path,
    monkeypatch,
    certification,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(session_factory, certification=certification)
    register_worker(
        client,
        "worker-a",
        engine_provenance=None,
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {},
    )
    payload = job_payload()

    preflight = client.post("/api/jobs/preflight", json=payload)
    create = client.post("/api/jobs", json=payload)

    assert preflight.status_code == 200
    assert not any(
        issue["code"].startswith("model_profile_certification")
        or issue["code"] == MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED
        for issue in preflight.json()["issues"]
    )
    assert create.status_code == 200, create.text


def test_certified_multi_agent_one_mismatch_blocks_both_paths(
    tmp_path,
    monkeypatch,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(),
    )
    register_worker(
        client,
        "worker-a",
        engine_provenance=exact_engine_provenance(),
    )
    register_worker(
        client,
        "worker-b",
        engine_provenance=exact_engine_provenance(
            model_revision="different-model"
        ),
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": PARSER_REVISION, "dirty": False},
    )

    assert_preflight_and_create_code(
        client,
        job_payload(allowed_server_ids=["worker-b"]),
        MODEL_PROFILE_CERTIFICATION_MISMATCH,
    )


def test_certified_all_exact_succeeds_on_both_paths(tmp_path, monkeypatch):
    client, session_factory = make_client_with_session(tmp_path)
    certification = certified_values(
        model_digest=SHA_B,
        runtime_revision="runtime-r1",
        layout_revision="layout-r1",
        layout_digest=SHA_C,
    )
    add_profile(session_factory, certification=certification)
    exact = exact_engine_provenance(
        model_digest=SHA_B,
        runtime_revision="runtime-r1",
        layout_revision="layout-r1",
        layout_digest=SHA_C,
    )
    register_worker(client, "worker-a", engine_provenance=exact)
    register_worker(client, "worker-b", engine_provenance=exact)
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": PARSER_REVISION, "dirty": False},
    )
    payload = job_payload(allowed_server_ids=["worker-b"])

    preflight = client.post("/api/jobs/preflight", json=payload)
    create = client.post("/api/jobs", json=payload)

    assert preflight.status_code == 200
    assert preflight.json()["ok"] is True
    assert create.status_code == 200, create.text


def test_certification_ignores_endpoint_port_and_api_key_and_never_leaks(
    tmp_path,
    monkeypatch,
):
    client, session_factory = make_client_with_session(tmp_path)
    leaked_endpoint = "10.0.0.8"
    leaked_key = "private-api-key-value"
    add_profile(
        session_factory,
        certification=certified_values(),
        ip=leaked_endpoint,
        port=31180,
        api_key=leaked_key,
    )
    register_worker(
        client,
        "worker-a",
        engine_provenance=exact_engine_provenance(
            model_revision="different-model"
        ),
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": PARSER_REVISION, "dirty": False},
    )
    payload = job_payload()

    preflight = client.post("/api/jobs/preflight", json=payload)
    create = client.post("/api/jobs", json=payload)
    rendered = preflight.text + create.text

    assert create.status_code == 400
    assert create.json()["detail"]["code"] == (
        MODEL_PROFILE_CERTIFICATION_MISMATCH
    )
    assert leaked_endpoint not in rendered
    assert leaked_key not in rendered
    assert "31180" not in rendered


@pytest.mark.parametrize(
    ("payload", "expected_detail"),
    [
        (
            job_payload(assigned_server_id="missing-worker"),
            "unknown assigned server: missing-worker",
        ),
        (
            job_payload(allowed_server_ids=["missing-worker"]),
            "unknown allowed server: missing-worker",
        ),
    ],
)
def test_create_preserves_unknown_explicit_server_error_priority(
    tmp_path,
    monkeypatch,
    payload,
    expected_detail,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(),
    )
    if payload.get("assigned_server_id") != "missing-worker":
        register_worker(
            client,
            "worker-a",
            engine_provenance=exact_engine_provenance(),
        )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {},
    )

    response = client.post("/api/jobs", json=payload)

    assert response.status_code == 400
    assert response.json() == {"detail": expected_detail}


@pytest.mark.parametrize(
    ("payload", "archived_server_id", "expected_detail"),
    [
        (
            job_payload(assigned_server_id="archived-worker"),
            "archived-worker",
            "unknown assigned server: archived-worker",
        ),
        (
            job_payload(allowed_server_ids=["archived-worker"]),
            "archived-worker",
            "unknown allowed server: archived-worker",
        ),
    ],
)
def test_create_preserves_archived_explicit_server_error_priority(
    tmp_path,
    monkeypatch,
    payload,
    archived_server_id,
    expected_detail,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(),
    )
    if payload.get("assigned_server_id") != archived_server_id:
        register_worker(
            client,
            "worker-a",
            engine_provenance=exact_engine_provenance(),
        )
    register_worker(
        client,
        archived_server_id,
        engine_provenance=exact_engine_provenance(),
    )
    with session_factory() as session:
        server = session.get(Server, archived_server_id)
        assert server is not None
        server.archived_at = datetime.now(timezone.utc)
        session.commit()
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {},
    )

    response = client.post("/api/jobs", json=payload)

    assert response.status_code == 400
    assert response.json() == {"detail": expected_detail}


def test_create_preserves_unknown_profile_error_and_preflight_issue(
    tmp_path,
):
    client, _ = make_client_with_session(tmp_path)
    register_worker(client, "worker-a", engine_provenance=None)
    payload = {
        **job_payload(),
        "model_profile_id": "missing-profile",
    }

    preflight = client.post("/api/jobs/preflight", json=payload)
    create = client.post("/api/jobs", json=payload)

    assert "unknown_model_profile" in {
        issue["code"] for issue in preflight.json()["issues"]
    }
    assert not any(
        issue["code"].startswith("model_profile_certification")
        for issue in preflight.json()["issues"]
    )
    assert create.status_code == 400
    assert create.json() == {
        "detail": "unknown model_profile_id: missing-profile"
    }


def test_pool_without_explicit_workers_requires_accessible_candidate(
    tmp_path,
    monkeypatch,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(),
    )
    register_worker(
        client,
        "worker-a",
        engine_provenance=exact_engine_provenance(),
        can_access=False,
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": PARSER_REVISION, "dirty": False},
    )
    payload = job_payload(
        assigned_server_id=None,
        input_mode="distributed_remote_folder_snapshot",
    )

    assert_preflight_and_create_code(
        client,
        payload,
        MODEL_PROFILE_CERTIFICATION_MISSING,
    )


def test_explicit_inaccessible_worker_is_still_certification_candidate(
    tmp_path,
    monkeypatch,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(),
    )
    register_worker(
        client,
        "worker-a",
        engine_provenance=exact_engine_provenance(),
        can_access=False,
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": PARSER_REVISION, "dirty": False},
    )
    payload = job_payload()

    preflight = client.post("/api/jobs/preflight", json=payload)
    create = client.post("/api/jobs", json=payload)

    assert "no_eligible_workers" in {
        issue["code"] for issue in preflight.json()["issues"]
    }
    assert not any(
        issue["code"].startswith("model_profile_certification")
        for issue in preflight.json()["issues"]
    )
    assert create.status_code == 200, create.text


def test_certification_gate_runs_before_job_or_pool_mutation(
    tmp_path,
    monkeypatch,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(),
    )
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {},
    )
    payload = job_payload(
        assigned_server_id=None,
        input_mode="distributed_remote_folder_snapshot",
    )

    response = client.post("/api/jobs", json=payload)

    assert response.status_code == 400
    with session_factory() as session:
        assert session.query(Server).filter(
            Server.id == "__server_pool__"
        ).count() == 0
        from ocr_platform.control.models import Job

        assert session.query(Job).count() == 0


@pytest.mark.parametrize(
    "capabilities_json",
    [
        "{",
        json.dumps(
            {
                "shared_paths": [
                    {
                        "path": "/shared",
                        "exists": True,
                        "is_dir": True,
                        "readable": True,
                        "writable": True,
                    }
                ],
                "engine_provenance": {"profiles": []},
            }
        ),
    ],
)
def test_certified_historical_invalid_capabilities_fail_closed_as_missing(
    tmp_path,
    monkeypatch,
    capabilities_json,
):
    client, session_factory = make_client_with_session(tmp_path)
    add_profile(
        session_factory,
        certification=certified_values(),
    )
    register_worker(
        client,
        "worker-a",
        engine_provenance=exact_engine_provenance(),
    )
    with session_factory() as session:
        server = session.get(Server, "worker-a")
        assert server is not None
        server.capabilities_json = capabilities_json
        session.commit()
    monkeypatch.setattr(
        "ocr_platform.legal.build_provenance",
        lambda: {"source_revision": PARSER_REVISION, "dirty": False},
    )

    assert_preflight_and_create_code(
        client,
        job_payload(),
        MODEL_PROFILE_CERTIFICATION_MISSING,
    )
