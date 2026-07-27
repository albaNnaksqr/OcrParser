"""Pure model-profile certification enforcement policy.

The policy accepts only already-sanitized revision/digest snapshots. It never
receives server ids, profile ids, endpoints, paths, credentials, or free-form
OCR content, so failures can be returned safely from public APIs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Sequence


MODEL_PROFILE_CERTIFICATION_MISSING = (
    "model_profile_certification_missing"
)
MODEL_PROFILE_CERTIFICATION_MISMATCH = (
    "model_profile_certification_mismatch"
)
MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED = (
    "model_profile_risk_acceptance_required"
)

_MISSING_MESSAGE = (
    "Model profile certification provenance is incomplete."
)
_MISMATCH_MESSAGE = (
    "Model profile certification provenance does not match the current "
    "Control build and candidate workers."
)
_RISK_MESSAGE = (
    "Verified model profile enforcement requires a complete risk acceptance "
    "record."
)
_CERTIFIED_REQUIRED_FIELDS = (
    "parser_revision",
    "model_revision",
    "runtime_digest",
    "fixture_set_digest",
    "evidence_digest",
)
_AGENT_REQUIRED_PROFILE_FIELDS = (
    "model_revision",
    "runtime_digest",
)
_AGENT_OPTIONAL_PROFILE_FIELDS = (
    "model_digest",
    "runtime_revision",
    "layout_revision",
    "layout_digest",
)


@dataclass(frozen=True)
class RiskAcceptanceSnapshot:
    accepted_by: str | None = None
    accepted_at: datetime | None = None
    reason: str | None = None


@dataclass(frozen=True)
class CertificationSnapshot:
    enforcement: str = "off"
    status: str = "contract_only"
    parser_revision: str | None = None
    parser_digest: str | None = None
    model_revision: str | None = None
    model_digest: str | None = None
    runtime_revision: str | None = None
    runtime_digest: str | None = None
    layout_revision: str | None = None
    layout_digest: str | None = None
    fixture_set_digest: str | None = None
    evidence_digest: str | None = None
    risk_acceptance: RiskAcceptanceSnapshot | None = None


@dataclass(frozen=True)
class BuildProvenanceSnapshot:
    source_revision: str | None = None
    dirty: bool | None = None


@dataclass(frozen=True)
class EngineProfileProvenanceSnapshot:
    model_revision: str | None = None
    model_digest: str | None = None
    runtime_revision: str | None = None
    runtime_digest: str | None = None
    layout_revision: str | None = None
    layout_digest: str | None = None


@dataclass(frozen=True)
class CandidateEngineProvenanceSnapshot:
    source_revision: str | None = None
    dirty: bool | None = None
    profile: EngineProfileProvenanceSnapshot | None = None


@dataclass(frozen=True)
class CertificationPolicyResult:
    allowed: bool
    code: str | None = None
    message: str | None = None
    details: dict[str, object] = field(default_factory=dict)


class ModelProfileCertificationError(ValueError):
    """Safe domain error returned by create-job certification enforcement."""

    code: str

    def __init__(self, result: CertificationPolicyResult) -> None:
        if result.allowed or result.code is None or result.message is None:
            raise ValueError("certification error requires a failed result")
        self.code = result.code
        self.message = result.message
        self.details = dict(result.details)
        super().__init__(result.code)

    def public_detail(self) -> dict[str, object]:
        return {
            "code": self.code,
            "message": self.message,
            "details": dict(self.details),
        }


class ModelProfileCertificationMissingError(
    ModelProfileCertificationError
):
    pass


class ModelProfileCertificationMismatchError(
    ModelProfileCertificationError
):
    pass


class ModelProfileRiskAcceptanceRequiredError(
    ModelProfileCertificationError
):
    pass


_ERROR_TYPES = {
    MODEL_PROFILE_CERTIFICATION_MISSING: (
        ModelProfileCertificationMissingError
    ),
    MODEL_PROFILE_CERTIFICATION_MISMATCH: (
        ModelProfileCertificationMismatchError
    ),
    MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED: (
        ModelProfileRiskAcceptanceRequiredError
    ),
}


def _allowed() -> CertificationPolicyResult:
    return CertificationPolicyResult(allowed=True)


def _failed(
    code: str,
    message: str,
    *,
    fields_key: str,
    fields: set[str],
    candidate_count: int | None = None,
    affected_candidate_count: int | None = None,
) -> CertificationPolicyResult:
    details: dict[str, object] = {
        fields_key: sorted(fields),
        "field_count": len(fields),
    }
    if candidate_count is not None:
        details["candidate_count"] = candidate_count
    if affected_candidate_count is not None:
        details["affected_candidate_count"] = affected_candidate_count
    return CertificationPolicyResult(
        allowed=False,
        code=code,
        message=message,
        details=details,
    )


def _missing(
    fields: set[str],
    *,
    candidate_count: int | None = None,
    affected_candidate_count: int | None = None,
) -> CertificationPolicyResult:
    return _failed(
        MODEL_PROFILE_CERTIFICATION_MISSING,
        _MISSING_MESSAGE,
        fields_key="missing_fields",
        fields=fields,
        candidate_count=candidate_count,
        affected_candidate_count=affected_candidate_count,
    )


def _mismatch(
    fields: set[str],
    *,
    candidate_count: int | None = None,
    affected_candidate_count: int | None = None,
) -> CertificationPolicyResult:
    return _failed(
        MODEL_PROFILE_CERTIFICATION_MISMATCH,
        _MISMATCH_MESSAGE,
        fields_key="mismatched_fields",
        fields=fields,
        candidate_count=candidate_count,
        affected_candidate_count=affected_candidate_count,
    )


def _present(value: str | None) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _risk_missing_fields(
    risk: RiskAcceptanceSnapshot | None,
) -> set[str]:
    if risk is None:
        return {"accepted_by", "accepted_at", "reason"}
    missing: set[str] = set()
    if not _present(risk.accepted_by):
        missing.add("accepted_by")
    if not _present(risk.reason):
        missing.add("reason")
    accepted_at = risk.accepted_at
    if (
        accepted_at is None
        or accepted_at.tzinfo is None
        or accepted_at.utcoffset() is None
    ):
        missing.add("accepted_at")
    return missing


def evaluate_model_profile_certification(
    certification: CertificationSnapshot | None,
    *,
    control_build: BuildProvenanceSnapshot,
    candidates: Sequence[CandidateEngineProvenanceSnapshot],
) -> CertificationPolicyResult:
    """Evaluate one profile against the current build and candidate workers."""

    if certification is None or certification.enforcement == "off":
        return _allowed()

    if certification.enforcement == "verified":
        if certification.status not in {"verified", "certified"}:
            return _mismatch({"status"})
        if certification.status == "verified":
            missing_risk = _risk_missing_fields(
                certification.risk_acceptance
            )
            if missing_risk:
                return _failed(
                    MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED,
                    _RISK_MESSAGE,
                    fields_key="missing_fields",
                    fields=missing_risk,
                )
        else:
            missing_certified_fields = {
                f"profile.{name}"
                for name in _CERTIFIED_REQUIRED_FIELDS
                if not _present(getattr(certification, name))
            }
            if missing_certified_fields:
                return _missing(missing_certified_fields)
        return _allowed()

    if certification.enforcement != "certified":
        return _mismatch({"enforcement"})
    if certification.status != "certified":
        return _mismatch({"status"})

    missing_fields = {
        f"profile.{name}"
        for name in _CERTIFIED_REQUIRED_FIELDS
        if not _present(getattr(certification, name))
    }
    if not _present(control_build.source_revision):
        missing_fields.add("control.source_revision")
    if control_build.dirty is None:
        missing_fields.add("control.dirty")
    if not candidates:
        missing_fields.add("candidate_workers")

    mismatch_fields: set[str] = set()
    if (
        _present(control_build.source_revision)
        and control_build.source_revision != certification.parser_revision
    ):
        mismatch_fields.add("control.source_revision")
    if control_build.dirty is True:
        mismatch_fields.add("control.dirty")

    missing_candidates = 0
    mismatched_candidates = 0
    for candidate in candidates:
        candidate_missing = False
        candidate_mismatch = False
        if not _present(candidate.source_revision):
            missing_fields.add("agent.source_revision")
            candidate_missing = True
        elif candidate.source_revision != certification.parser_revision:
            mismatch_fields.add("agent.source_revision")
            candidate_mismatch = True
        if candidate.dirty is None:
            missing_fields.add("agent.dirty")
            candidate_missing = True
        elif candidate.dirty is True:
            mismatch_fields.add("agent.dirty")
            candidate_mismatch = True
        if candidate.profile is None:
            missing_fields.add("agent.profile")
            candidate_missing = True
        else:
            for name in _AGENT_REQUIRED_PROFILE_FIELDS:
                actual = getattr(candidate.profile, name)
                expected = getattr(certification, name)
                if not _present(actual):
                    missing_fields.add(f"agent.{name}")
                    candidate_missing = True
                elif actual != expected:
                    mismatch_fields.add(f"agent.{name}")
                    candidate_mismatch = True
            for name in _AGENT_OPTIONAL_PROFILE_FIELDS:
                actual = getattr(candidate.profile, name)
                expected = getattr(certification, name)
                actual_present = _present(actual)
                expected_present = _present(expected)
                if expected_present and not actual_present:
                    missing_fields.add(f"agent.{name}")
                    candidate_missing = True
                elif actual_present != expected_present or (
                    actual_present and actual != expected
                ):
                    mismatch_fields.add(f"agent.{name}")
                    candidate_mismatch = True
        if candidate_missing:
            missing_candidates += 1
        if candidate_mismatch:
            mismatched_candidates += 1

    if missing_fields:
        return _missing(
            missing_fields,
            candidate_count=len(candidates),
            affected_candidate_count=missing_candidates,
        )
    if mismatch_fields:
        return _mismatch(
            mismatch_fields,
            candidate_count=len(candidates),
            affected_candidate_count=mismatched_candidates,
        )
    return _allowed()


def raise_for_certification_result(
    result: CertificationPolicyResult,
) -> None:
    if result.allowed:
        return
    error_type = _ERROR_TYPES.get(result.code)
    if error_type is None:
        raise ValueError("unknown certification policy result")
    raise error_type(result)


__all__ = [
    "MODEL_PROFILE_CERTIFICATION_MISMATCH",
    "MODEL_PROFILE_CERTIFICATION_MISSING",
    "MODEL_PROFILE_RISK_ACCEPTANCE_REQUIRED",
    "BuildProvenanceSnapshot",
    "CandidateEngineProvenanceSnapshot",
    "CertificationPolicyResult",
    "CertificationSnapshot",
    "EngineProfileProvenanceSnapshot",
    "ModelProfileCertificationError",
    "ModelProfileCertificationMismatchError",
    "ModelProfileCertificationMissingError",
    "ModelProfileRiskAcceptanceRequiredError",
    "RiskAcceptanceSnapshot",
    "evaluate_model_profile_certification",
    "raise_for_certification_result",
]
