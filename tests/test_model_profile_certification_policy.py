from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from ocr_platform.control.domains.model_profiles.policy import (
    MODEL_PROFILE_CERTIFICATION_MISMATCH,
    MODEL_PROFILE_CERTIFICATION_MISSING,
    MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED,
    BuildProvenanceSnapshot,
    CandidateEngineProvenanceSnapshot,
    CertificationSnapshot,
    EngineProfileProvenanceSnapshot,
    ModelProfileCertificationMismatchError,
    ModelProfileCertificationMissingError,
    ModelProfileRiskAcceptanceRequiredError,
    RiskAcceptanceSnapshot,
    evaluate_model_profile_certification,
    raise_for_certification_result,
)


SHA_A = f"sha256:{'a' * 64}"
SHA_B = f"sha256:{'b' * 64}"
PARSER_REVISION = "47e1c0399db97f4ec48715548b8c937bc77c20ba"


def certified_snapshot(**overrides) -> CertificationSnapshot:
    values = {
        "enforcement": "certified",
        "status": "certified",
        "parser_revision": PARSER_REVISION,
        "model_revision": "model-r1",
        "runtime_digest": SHA_A,
        "fixture_set_digest": SHA_B,
        "evidence_digest": SHA_A,
    }
    values.update(overrides)
    return CertificationSnapshot(**values)


def exact_candidate(**overrides) -> CandidateEngineProvenanceSnapshot:
    profile_values = {
        "model_revision": "model-r1",
        "runtime_digest": SHA_A,
    }
    profile_values.update(overrides.pop("profile", {}))
    values = {
        "source_revision": PARSER_REVISION,
        "dirty": False,
        "profile": EngineProfileProvenanceSnapshot(**profile_values),
    }
    values.update(overrides)
    return CandidateEngineProvenanceSnapshot(**values)


def evaluate(
    certification: CertificationSnapshot | None,
    *,
    build: BuildProvenanceSnapshot | None = None,
    candidates: list[CandidateEngineProvenanceSnapshot] | None = None,
):
    return evaluate_model_profile_certification(
        certification,
        control_build=build or BuildProvenanceSnapshot(
            source_revision=PARSER_REVISION,
            dirty=False,
        ),
        candidates=candidates if candidates is not None else [exact_candidate()],
    )


@pytest.mark.parametrize(
    "certification",
    [
        None,
        CertificationSnapshot(enforcement="off", status="contract_only"),
        certified_snapshot(enforcement="off"),
    ],
)
def test_policy_enforcement_off_always_allows(certification):
    result = evaluate(
        certification,
        build=BuildProvenanceSnapshot(),
        candidates=[],
    )

    assert result.allowed is True
    assert result.code is None


@pytest.mark.parametrize(
    ("risk", "missing_fields"),
    [
        (None, {"accepted_by", "accepted_at", "reason"}),
        (
            RiskAcceptanceSnapshot(
                accepted_by="",
                accepted_at=datetime.now(timezone.utc),
                reason="accepted",
            ),
            {"accepted_by"},
        ),
        (
            RiskAcceptanceSnapshot(
                accepted_by="operator",
                accepted_at=datetime(2026, 7, 28, 9, 0, 0),
                reason="accepted",
            ),
            {"accepted_at"},
        ),
        (
            RiskAcceptanceSnapshot(
                accepted_by="operator",
                accepted_at=datetime.now(timezone.utc),
                reason=" ",
            ),
            {"reason"},
        ),
    ],
)
def test_policy_verified_requires_complete_aware_risk(
    risk,
    missing_fields,
):
    result = evaluate(
        CertificationSnapshot(
            enforcement="verified",
            status="verified",
            risk_acceptance=risk,
        ),
    )

    assert result.allowed is False
    assert result.code == MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED
    assert set(result.details["missing_fields"]) == missing_fields


@pytest.mark.parametrize(
    "certification",
    [
        CertificationSnapshot(
            enforcement="verified",
            status="verified",
            risk_acceptance=RiskAcceptanceSnapshot(
                accepted_by="release-operator",
                accepted_at=datetime.now(timezone.utc),
                reason="approved-for-rollout",
            ),
        ),
        certified_snapshot(enforcement="verified"),
    ],
)
def test_policy_verified_accepts_verified_or_certified_without_agent_exactness(
    certification,
):
    result = evaluate(
        certification,
        build=BuildProvenanceSnapshot(
            source_revision="different-build",
            dirty=True,
        ),
        candidates=[],
    )

    assert result.allowed is True


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
def test_policy_verified_certified_requires_five_profile_fields(missing):
    result = evaluate(
        certified_snapshot(
            enforcement="verified",
            **{missing: None},
        ),
        build=BuildProvenanceSnapshot(),
        candidates=[],
    )

    assert result.code == MODEL_PROFILE_CERTIFICATION_MISSING
    assert result.details["missing_fields"] == [f"profile.{missing}"]


def test_policy_verified_rejects_other_statuses_as_mismatch():
    result = evaluate(
        CertificationSnapshot(
            enforcement="verified",
            status="contract_only",
        )
    )

    assert result.code == MODEL_PROFILE_CERTIFICATION_MISMATCH
    assert result.details["mismatched_fields"] == ["status"]


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
def test_policy_certified_requires_five_profile_fields(missing):
    result = evaluate(certified_snapshot(**{missing: None}))

    assert result.code == MODEL_PROFILE_CERTIFICATION_MISSING
    assert f"profile.{missing}" in result.details["missing_fields"]


@pytest.mark.parametrize(
    ("build", "code", "field"),
    [
        (
            BuildProvenanceSnapshot(source_revision=None, dirty=False),
            MODEL_PROFILE_CERTIFICATION_MISSING,
            "control.source_revision",
        ),
        (
            BuildProvenanceSnapshot(
                source_revision=PARSER_REVISION,
                dirty=None,
            ),
            MODEL_PROFILE_CERTIFICATION_MISSING,
            "control.dirty",
        ),
        (
            BuildProvenanceSnapshot(
                source_revision="different-parser",
                dirty=False,
            ),
            MODEL_PROFILE_CERTIFICATION_MISMATCH,
            "control.source_revision",
        ),
        (
            BuildProvenanceSnapshot(
                source_revision=PARSER_REVISION,
                dirty=True,
            ),
            MODEL_PROFILE_CERTIFICATION_MISMATCH,
            "control.dirty",
        ),
    ],
)
def test_policy_certified_checks_immutable_control_build(build, code, field):
    result = evaluate(certified_snapshot(), build=build)

    assert result.code == code
    fields = result.details.get("missing_fields") or result.details.get(
        "mismatched_fields"
    )
    assert field in fields


@pytest.mark.parametrize(
    ("candidate", "field"),
    [
        (
            CandidateEngineProvenanceSnapshot(
                source_revision=None,
                dirty=False,
                profile=exact_candidate().profile,
            ),
            "agent.source_revision",
        ),
        (
            CandidateEngineProvenanceSnapshot(
                source_revision=PARSER_REVISION,
                dirty=None,
                profile=exact_candidate().profile,
            ),
            "agent.dirty",
        ),
        (
            CandidateEngineProvenanceSnapshot(
                source_revision=PARSER_REVISION,
                dirty=False,
                profile=None,
            ),
            "agent.profile",
        ),
        (
            exact_candidate(profile={"model_revision": None}),
            "agent.model_revision",
        ),
        (
            exact_candidate(profile={"runtime_digest": None}),
            "agent.runtime_digest",
        ),
    ],
)
def test_policy_certified_missing_agent_provenance(candidate, field):
    result = evaluate(certified_snapshot(), candidates=[candidate])

    assert result.code == MODEL_PROFILE_CERTIFICATION_MISSING
    assert field in result.details["missing_fields"]
    assert result.details["candidate_count"] == 1
    assert result.details["affected_candidate_count"] == 1


def test_policy_certified_requires_at_least_one_candidate():
    result = evaluate(certified_snapshot(), candidates=[])

    assert result.code == MODEL_PROFILE_CERTIFICATION_MISSING
    assert result.details["missing_fields"] == ["candidate_workers"]
    assert result.details["candidate_count"] == 0


@pytest.mark.parametrize(
    ("candidate", "field"),
    [
        (
            exact_candidate(source_revision="different-parser"),
            "agent.source_revision",
        ),
        (exact_candidate(dirty=True), "agent.dirty"),
        (
            exact_candidate(profile={"model_revision": "different-model"}),
            "agent.model_revision",
        ),
        (
            exact_candidate(
                profile={
                    "runtime_digest": f"sha256:{'c' * 64}",
                }
            ),
            "agent.runtime_digest",
        ),
    ],
)
def test_policy_certified_detects_agent_mismatch(candidate, field):
    result = evaluate(certified_snapshot(), candidates=[candidate])

    assert result.code == MODEL_PROFILE_CERTIFICATION_MISMATCH
    assert field in result.details["mismatched_fields"]


@pytest.mark.parametrize(
    "field",
    [
        "model_digest",
        "runtime_revision",
        "layout_revision",
        "layout_digest",
    ],
)
def test_policy_certified_checks_each_present_optional_profile_field(field):
    expected = SHA_B if field.endswith("digest") else "revision-r2"
    certification = certified_snapshot(**{field: expected})
    missing = evaluate(
        certification,
        candidates=[exact_candidate(profile={field: None})],
    )
    mismatch = evaluate(
        certification,
        candidates=[exact_candidate(profile={field: "different"})],
    )
    exact = evaluate(
        certification,
        candidates=[exact_candidate(profile={field: expected})],
    )

    assert missing.code == MODEL_PROFILE_CERTIFICATION_MISSING
    assert f"agent.{field}" in missing.details["missing_fields"]
    assert mismatch.code == MODEL_PROFILE_CERTIFICATION_MISMATCH
    assert f"agent.{field}" in mismatch.details["mismatched_fields"]
    assert exact.allowed is True


@pytest.mark.parametrize(
    "field",
    [
        "model_digest",
        "runtime_revision",
        "layout_revision",
        "layout_digest",
    ],
)
def test_policy_certified_rejects_unrecorded_optional_agent_field(field):
    actual = SHA_B if field.endswith("digest") else "revision-r2"

    result = evaluate(
        certified_snapshot(),
        candidates=[exact_candidate(profile={field: actual})],
    )

    assert result.code == MODEL_PROFILE_CERTIFICATION_MISMATCH
    assert f"agent.{field}" in result.details["mismatched_fields"]


def test_policy_certified_rejects_one_mismatch_across_multiple_agents():
    result = evaluate(
        certified_snapshot(),
        candidates=[
            exact_candidate(),
            exact_candidate(profile={"model_revision": "different-model"}),
        ],
    )

    assert result.code == MODEL_PROFILE_CERTIFICATION_MISMATCH
    assert result.details["candidate_count"] == 2
    assert result.details["affected_candidate_count"] == 1


def test_policy_missing_precedes_mismatch_across_mixed_candidates():
    result = evaluate(
        certified_snapshot(),
        candidates=[
            CandidateEngineProvenanceSnapshot(),
            exact_candidate(profile={"model_revision": "different-model"}),
        ],
    )

    assert result.code == MODEL_PROFILE_CERTIFICATION_MISSING
    assert "agent.profile" in result.details["missing_fields"]
    assert "mismatched_fields" not in result.details
    assert result.details["candidate_count"] == 2
    assert result.details["affected_candidate_count"] == 1


def test_policy_certified_all_exact_succeeds():
    certification = certified_snapshot(
        model_digest=SHA_B,
        runtime_revision="runtime-r1",
        layout_revision="layout-r1",
        layout_digest=SHA_A,
    )
    result = evaluate(
        certification,
        candidates=[
            exact_candidate(
                profile={
                    "model_digest": SHA_B,
                    "runtime_revision": "runtime-r1",
                    "layout_revision": "layout-r1",
                    "layout_digest": SHA_A,
                }
            ),
            exact_candidate(
                profile={
                    "model_digest": SHA_B,
                    "runtime_revision": "runtime-r1",
                    "layout_revision": "layout-r1",
                    "layout_digest": SHA_A,
                }
            ),
        ],
    )

    assert result.allowed is True


@pytest.mark.parametrize(
    ("result", "error_type"),
    [
        (
            evaluate(
                certified_snapshot(),
                build=BuildProvenanceSnapshot(),
            ),
            ModelProfileCertificationMissingError,
        ),
        (
            evaluate(
                certified_snapshot(),
                build=BuildProvenanceSnapshot(
                    source_revision="different",
                    dirty=False,
                ),
            ),
            ModelProfileCertificationMismatchError,
        ),
        (
            evaluate(
                CertificationSnapshot(
                    enforcement="verified",
                    status="verified",
                )
            ),
            ModelProfileRiskAcceptanceRequiredError,
        ),
    ],
)
def test_policy_raises_typed_safe_domain_errors(result, error_type):
    with pytest.raises(error_type) as exc_info:
        raise_for_certification_result(result)

    detail = exc_info.value.public_detail()
    rendered = json.dumps(detail, sort_keys=True)
    assert detail["code"] == result.code
    assert detail["details"] == result.details
    for forbidden in [
        "server-a",
        "profile-a",
        "10.0.0.8",
        "/private/model",
        "api-key-value",
        "different-parser",
        PARSER_REVISION,
        SHA_A,
    ]:
        assert forbidden not in rendered
