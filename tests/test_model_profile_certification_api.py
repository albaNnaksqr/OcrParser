from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from ocr_platform.control.app import create_app
from ocr_platform.control.database import create_session_factory, init_db
from ocr_platform.control.models import (
    ModelProfile,
    ModelProfileCertification,
)


SHA_A = "sha256:" + "a" * 64
SHA_B = "sha256:" + "b" * 64
SHA_C = "sha256:" + "c" * 64
UNSAFE_TEXT_VALUES = [
    "api_key=must-not-be-stored",
    "https://private.example/evidence",
    "192.0.2.91",
    "192.0.2.91:31180",
    "2001:db8::1",
    "[fd00::1]:8080",
    "model.internal",
    "model.corp",
    "model.local",
    "model.lan",
    "model.svc",
    "model.svc:8000",
    "model.private",
    "model.intra:8000",
    "localhost",
    "localhost:8000",
    "mineru:8000",
    "model.example:8000",
    "/data/models/revision",
    "/mnt/models/revision",
    "/opt/models/revision",
    "~/models/revision",
    r"\\server\share\model",
    r"C:\models\revision",
    "D:/models/revision",
]


def make_client_with_session(tmp_path):
    session_factory, engine = create_session_factory(
        f"sqlite:///{tmp_path / 'control.db'}"
    )
    init_db(engine)
    app = create_app(session_factory=session_factory)
    return TestClient(app), session_factory


def profile_payload(**overrides):
    payload = {
        "label": "Certification test profile",
        "engine": "dotsocr",
        "model_name": "DotsOCR",
        "extra_args": {},
    }
    payload.update(overrides)
    return payload


def certified_payload():
    return {
        "enforcement": "certified",
        "status": "certified",
        "parser_revision": "47e1c0399db97f4ec48715548b8c937bc77c20ba",
        "model_revision": "model-r1",
        "runtime_digest": SHA_A,
        "fixture_set_digest": SHA_B,
        "evidence_digest": SHA_C,
    }


def test_missing_certification_row_is_synthesized_without_backfill(
    tmp_path,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)

    response = client.get("/api/model-profiles")

    assert response.status_code == 200
    profile = next(
        item for item in response.json() if item["id"] == "dotsocr_15"
    )
    assert profile["certification"] == {
        "enforcement": "off",
        "status": "contract_only",
        "parser_revision": None,
        "parser_digest": None,
        "model_revision": None,
        "model_digest": None,
        "runtime_revision": None,
        "runtime_digest": None,
        "layout_revision": None,
        "layout_digest": None,
        "fixture_set_digest": None,
        "evidence_digest": None,
        "certified_at": None,
        "risk_acceptance": None,
        "updated_at": None,
    }
    with session_factory() as session:
        assert (
            session.get(ModelProfileCertification, "dotsocr_15") is None
        )


def test_new_profile_without_certification_does_not_create_bridge_row(
    tmp_path,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/new-contract-only",
        json=profile_payload(),
    )

    assert response.status_code == 200
    assert response.json()["certification"]["status"] == "contract_only"
    assert response.json()["certification"]["enforcement"] == "off"
    with session_factory() as session:
        assert session.get(ModelProfile, "new-contract-only") is not None
        assert (
            session.get(
                ModelProfileCertification,
                "new-contract-only",
            )
            is None
        )


def test_certified_profile_requires_and_persists_immutable_provenance(
    tmp_path,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/certified-profile",
        json=profile_payload(certification=certified_payload()),
    )

    assert response.status_code == 200
    certification = response.json()["certification"]
    assert certification["status"] == "certified"
    assert certification["enforcement"] == "certified"
    assert certification["parser_revision"].startswith("47e1c0")
    assert certification["runtime_digest"] == SHA_A
    assert certification["updated_at"] is not None
    with session_factory() as session:
        stored = session.get(
            ModelProfileCertification,
            "certified-profile",
        )
        assert stored is not None
        assert stored.status == "certified"
        assert stored.runtime_digest == SHA_A
        assert stored.risk_acceptance_json == "{}"


@pytest.mark.parametrize(
    "missing",
    [
        "parser_revision",
        "model_revision",
        "runtime_digest",
        "fixture_set_digest",
        "evidence_digest",
    ],
)
def test_certified_profile_rejects_missing_required_provenance(
    tmp_path,
    missing,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)
    certification = certified_payload()
    certification.pop(missing)

    response = client.put(
        "/api/model-profiles/incomplete-certified",
        json=profile_payload(certification=certification),
    )

    assert response.status_code == 400
    assert missing in response.json()["detail"]
    with session_factory() as session:
        assert session.get(ModelProfile, "incomplete-certified") is None
        assert (
            session.get(
                ModelProfileCertification,
                "incomplete-certified",
            )
            is None
        )


def test_omitted_or_null_certification_preserves_existing_record(
    tmp_path,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)
    first = client.put(
        "/api/model-profiles/preserved",
        json=profile_payload(certification=certified_payload()),
    )
    assert first.status_code == 200

    omitted = client.put(
        "/api/model-profiles/preserved",
        json=profile_payload(label="Updated without certification"),
    )
    explicit_null = client.put(
        "/api/model-profiles/preserved",
        json=profile_payload(
            label="Updated with null certification",
            certification=None,
        ),
    )

    assert omitted.status_code == 200
    assert explicit_null.status_code == 200
    assert omitted.json()["certification"]["status"] == "certified"
    assert explicit_null.json()["certification"]["runtime_digest"] == SHA_A
    with session_factory() as session:
        stored = session.get(ModelProfileCertification, "preserved")
        assert stored is not None
        assert stored.status == "certified"
        assert stored.runtime_digest == SHA_A


def test_explicit_contract_only_update_clears_previous_provenance(
    tmp_path,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)
    first = client.put(
        "/api/model-profiles/cleared-certification",
        json=profile_payload(
            certification={
                **certified_payload(),
                "risk_acceptance": {
                    "accepted_by": "Release Operator",
                    "accepted_at": "2026-07-28T09:00:00+08:00",
                    "reason": "Temporary release acceptance.",
                },
            }
        ),
    )
    assert first.status_code == 200

    cleared = client.put(
        "/api/model-profiles/cleared-certification",
        json=profile_payload(
            certification={
                "enforcement": "off",
                "status": "contract_only",
            }
        ),
    )

    assert cleared.status_code == 200
    certification = cleared.json()["certification"]
    assert certification["status"] == "contract_only"
    assert certification["enforcement"] == "off"
    assert certification["parser_revision"] is None
    assert certification["model_revision"] is None
    assert certification["runtime_digest"] is None
    assert certification["fixture_set_digest"] is None
    assert certification["evidence_digest"] is None
    assert certification["risk_acceptance"] is None
    with session_factory() as session:
        stored = session.get(
            ModelProfileCertification,
            "cleared-certification",
        )
        assert stored is not None
        assert stored.parser_revision is None
        assert stored.runtime_digest is None
        assert stored.risk_acceptance_json == "{}"


def test_verified_enforcement_requires_timezone_aware_risk_acceptance(
    tmp_path,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)
    certification = {
        "enforcement": "verified",
        "status": "verified",
        "parser_revision": "parser-r1",
        "model_revision": "model-r1",
    }

    missing = client.put(
        "/api/model-profiles/verified-profile",
        json=profile_payload(certification=certification),
    )
    naive = client.put(
        "/api/model-profiles/verified-profile",
        json=profile_payload(
            certification={
                **certification,
                "risk_acceptance": {
                    "accepted_by": "Release Operator",
                    "accepted_at": "2026-07-28T09:00:00",
                    "reason": "Pinned service is verified for this rollout.",
                },
            }
        ),
    )
    valid = client.put(
        "/api/model-profiles/verified-profile",
        json=profile_payload(
            certification={
                **certification,
                "risk_acceptance": {
                    "accepted_by": "Release Operator",
                    "accepted_at": "2026-07-28T09:00:00+08:00",
                    "reason": "Pinned service is verified for this rollout.",
                },
            }
        ),
    )

    assert missing.status_code == 400
    assert "requires risk_acceptance" in missing.json()["detail"]
    assert naive.status_code == 422
    assert valid.status_code == 200
    risk = valid.json()["certification"]["risk_acceptance"]
    assert risk["accepted_by"] == "Release Operator"
    assert risk["accepted_at"].endswith("Z")
    with session_factory() as session:
        stored = session.get(
            ModelProfileCertification,
            "verified-profile",
        )
        assert stored is not None
        assert json.loads(stored.risk_acceptance_json) == {
            "accepted_at": "2026-07-28T01:00:00+00:00",
            "accepted_by": "Release Operator",
            "reason": "Pinned service is verified for this rollout.",
        }


def test_verified_status_is_not_automatically_promoted(tmp_path) -> None:
    client, _ = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/verified-informational",
        json=profile_payload(
            certification={
                "enforcement": "off",
                "status": "verified",
                "parser_revision": "parser-r1",
                "model_revision": "model-r1",
            }
        ),
    )

    assert response.status_code == 200
    assert response.json()["certification"]["status"] == "verified"
    assert response.json()["certification"]["enforcement"] == "off"


def test_certified_at_requires_timezone_when_present(tmp_path) -> None:
    client, _ = make_client_with_session(tmp_path)
    certification = certified_payload()
    certification["certified_at"] = "2026-07-28T09:00:00"

    response = client.put(
        "/api/model-profiles/naive-certified-at",
        json=profile_payload(certification=certification),
    )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "forbidden_field",
    ["api_key", "endpoint", "address", "private_path", "ocr_text"],
)
def test_certification_contract_forbids_non_whitelisted_fields(
    tmp_path,
    forbidden_field,
) -> None:
    client, _ = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/forbidden-field",
        json=profile_payload(
            certification={
                "status": "contract_only",
                forbidden_field: "must-not-be-stored",
            }
        ),
    )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("runtime_digest", "sha256:not-a-real-digest"),
        ("parser_revision", "https://private.example/revision"),
        ("model_revision", "/private/models/revision"),
    ],
)
def test_certification_rejects_nonimmutable_or_address_values(
    tmp_path,
    field,
    value,
) -> None:
    client, _ = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/unsafe-value",
        json=profile_payload(
            certification={
                "status": "contract_only",
                field: value,
            }
        ),
    )

    assert response.status_code == 400
    assert field in response.json()["detail"]


@pytest.mark.parametrize(
    "unsafe_value",
    UNSAFE_TEXT_VALUES,
)
def test_risk_acceptance_rejects_sensitive_values(
    tmp_path,
    unsafe_value,
) -> None:
    client, _ = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/unsafe-risk",
        json=profile_payload(
            certification={
                "status": "verified",
                "enforcement": "verified",
                "risk_acceptance": {
                    "accepted_by": "Release Operator",
                    "accepted_at": "2026-07-28T09:00:00+08:00",
                    "reason": unsafe_value,
                },
            }
        ),
    )

    assert response.status_code == 400
    assert "may not contain" in response.json()["detail"]
    assert unsafe_value not in response.json()["detail"]


@pytest.mark.parametrize("unsafe_value", UNSAFE_TEXT_VALUES)
def test_revision_rejects_sensitive_values_without_echo(
    tmp_path,
    unsafe_value,
) -> None:
    client, _ = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/unsafe-revision",
        json=profile_payload(
            certification={
                "status": "contract_only",
                "parser_revision": unsafe_value,
            }
        ),
    )

    assert response.status_code == 400
    assert "parser_revision" in response.json()["detail"]
    assert unsafe_value not in response.json()["detail"]


@pytest.mark.parametrize(
    "reason",
    [
        (
            "Release note: API key unavailable on 2026-07-28; "
            "use the verified deployment."
        ),
        "Maintenance window 09:00 to 10:00; API key unavailable.",
    ],
)
def test_normal_risk_language_is_not_treated_as_a_secret(
    tmp_path,
    reason,
) -> None:
    client, _ = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/normal-risk-language",
        json=profile_payload(
            certification={
                "status": "verified",
                "enforcement": "verified",
                "risk_acceptance": {
                    "accepted_by": "Release Operator",
                    "accepted_at": "2026-07-28T09:00:00+08:00",
                    "reason": reason,
                },
            }
        ),
    )

    assert response.status_code == 200


def test_risk_acceptance_name_uses_the_same_sensitive_value_filter(
    tmp_path,
) -> None:
    client, _ = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/unsafe-acceptor",
        json=profile_payload(
            certification={
                "status": "verified",
                "enforcement": "verified",
                "risk_acceptance": {
                    "accepted_by": r"\\private-host\operators\name",
                    "accepted_at": "2026-07-28T09:00:00+08:00",
                    "reason": "Temporary acceptance.",
                },
            }
        ),
    )

    assert response.status_code == 400
    assert "risk_acceptance.accepted_by" in response.json()["detail"]
    assert "private-host" not in response.json()["detail"]


@pytest.mark.parametrize(
    "risk_acceptance",
    [
        {
            "accepted_by": " ",
            "accepted_at": "2026-07-28T09:00:00+08:00",
            "reason": "Valid reason.",
        },
        {
            "accepted_by": "Release Operator",
            "accepted_at": "2026-07-28T09:00:00+08:00",
            "reason": " ",
        },
        {
            "accepted_by": "Release Operator",
            "accepted_at": "2026-07-28T09:00:00+08:00",
            "reason": "Valid reason.",
            "endpoint": "private.example",
        },
    ],
)
def test_risk_acceptance_contract_is_strict_and_nonempty(
    tmp_path,
    risk_acceptance,
) -> None:
    client, _ = make_client_with_session(tmp_path)

    response = client.put(
        "/api/model-profiles/invalid-risk",
        json=profile_payload(
            certification={
                "status": "verified",
                "enforcement": "verified",
                "risk_acceptance": risk_acceptance,
            }
        ),
    )

    assert response.status_code == 422


def test_malformed_historical_risk_json_is_not_exposed_or_fatal(
    tmp_path,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)
    with session_factory() as session:
        profile = ModelProfile(
            id="legacy-risk",
            label="Legacy risk",
            engine="dotsocr",
        )
        profile.certification = ModelProfileCertification(
            profile_id=profile.id,
            enforcement="verified",
            status="verified",
            risk_acceptance_json='{"api_key":"must-not-leak"}',
        )
        session.add(profile)
        session.commit()

    response = client.get("/api/model-profiles")

    assert response.status_code == 200
    profile = next(
        item for item in response.json() if item["id"] == "legacy-risk"
    )
    assert profile["certification"]["risk_acceptance"] is None
    assert "must-not-leak" not in response.text


def test_syntactically_broken_historical_risk_json_is_not_fatal(
    tmp_path,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)
    with session_factory() as session:
        profile = ModelProfile(
            id="legacy-broken-risk",
            label="Legacy broken risk",
            engine="dotsocr",
        )
        profile.certification = ModelProfileCertification(
            profile_id=profile.id,
            enforcement="verified",
            status="verified",
            risk_acceptance_json='{"accepted_by":',
        )
        session.add(profile)
        session.commit()

    response = client.get("/api/model-profiles")

    assert response.status_code == 200
    profile = next(
        item for item in response.json() if item["id"] == "legacy-broken-risk"
    )
    assert profile["certification"]["risk_acceptance"] is None


@pytest.mark.parametrize("unsafe_value", UNSAFE_TEXT_VALUES)
def test_unsafe_historical_provenance_and_risk_are_not_exposed(
    tmp_path,
    unsafe_value,
) -> None:
    client, session_factory = make_client_with_session(tmp_path)
    with session_factory() as session:
        profile = ModelProfile(
            id="legacy-unsafe-provenance",
            label="Legacy unsafe provenance",
            engine="dotsocr",
        )
        profile.certification = ModelProfileCertification(
            profile_id=profile.id,
            enforcement="off",
            status="verified",
            parser_revision=unsafe_value,
            risk_acceptance_json=json.dumps(
                {
                    "accepted_by": "Release Operator",
                    "accepted_at": "2026-07-28T09:00:00+08:00",
                    "reason": unsafe_value,
                }
            ),
        )
        session.add(profile)
        session.commit()

    response = client.get("/api/model-profiles")

    assert response.status_code == 200
    profile = next(
        item
        for item in response.json()
        if item["id"] == "legacy-unsafe-provenance"
    )
    assert profile["certification"]["parser_revision"] is None
    assert profile["certification"]["risk_acceptance"] is None
    assert unsafe_value not in response.text
