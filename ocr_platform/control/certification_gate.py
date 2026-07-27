"""Shared application adapter for model-profile certification enforcement."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from ocr_platform import legal
from ocr_platform.engine_provenance import (
    EngineProvenanceError,
    sanitize_engine_provenance,
)

from .domains.common import json_loads_object
from .domains.model_profiles import certification as certification_contract
from .domains.model_profiles import policy
from .models import ModelProfile, Server
from .schemas import JobCreateRequest


def _certification_snapshot(
    profile: ModelProfile,
) -> policy.CertificationSnapshot | None:
    certification = profile.certification
    if certification is None:
        return None
    try:
        response = certification_contract.certification_to_response(
            certification
        )
    except (TypeError, ValueError):
        return policy.CertificationSnapshot(
            enforcement=str(certification.enforcement or ""),
            status=str(certification.status or ""),
        )
    risk = response.risk_acceptance
    return policy.CertificationSnapshot(
        enforcement=response.enforcement,
        status=response.status,
        parser_revision=response.parser_revision,
        parser_digest=response.parser_digest,
        model_revision=response.model_revision,
        model_digest=response.model_digest,
        runtime_revision=response.runtime_revision,
        runtime_digest=response.runtime_digest,
        layout_revision=response.layout_revision,
        layout_digest=response.layout_digest,
        fixture_set_digest=response.fixture_set_digest,
        evidence_digest=response.evidence_digest,
        risk_acceptance=(
            policy.RiskAcceptanceSnapshot(
                accepted_by=risk.accepted_by,
                accepted_at=risk.accepted_at,
                reason=risk.reason,
            )
            if risk is not None
            else None
        ),
    )


def _build_snapshot() -> policy.BuildProvenanceSnapshot:
    build = legal.build_provenance()
    source_revision = build.get("source_revision")
    dirty = build.get("dirty")
    return policy.BuildProvenanceSnapshot(
        source_revision=(
            source_revision if isinstance(source_revision, str) else None
        ),
        dirty=dirty if type(dirty) is bool else None,
    )


def _candidate_snapshot(
    server: Server | None,
    *,
    profile_id: str,
) -> policy.CandidateEngineProvenanceSnapshot:
    if server is None:
        return policy.CandidateEngineProvenanceSnapshot()
    try:
        capabilities = json_loads_object(server.capabilities_json)
        engine_provenance = sanitize_engine_provenance(
            capabilities.get("engine_provenance", {"profiles": {}})
        )
    except (EngineProvenanceError, TypeError, ValueError):
        return policy.CandidateEngineProvenanceSnapshot()
    raw_profiles = engine_provenance.get("profiles")
    raw_profile = (
        raw_profiles.get(profile_id)
        if isinstance(raw_profiles, dict)
        else None
    )
    profile = (
        policy.EngineProfileProvenanceSnapshot(
            model_revision=raw_profile.get("model_revision"),
            model_digest=raw_profile.get("model_digest"),
            runtime_revision=raw_profile.get("runtime_revision"),
            runtime_digest=raw_profile.get("runtime_digest"),
            layout_revision=raw_profile.get("layout_revision"),
            layout_digest=raw_profile.get("layout_digest"),
        )
        if isinstance(raw_profile, dict)
        else None
    )
    source_revision = engine_provenance.get("source_revision")
    dirty = engine_provenance.get("dirty")
    return policy.CandidateEngineProvenanceSnapshot(
        source_revision=(
            source_revision if isinstance(source_revision, str) else None
        ),
        dirty=dirty if type(dirty) is bool else None,
        profile=profile,
    )


def evaluate_job_model_profile_certification(
    session: Session,
    request: JobCreateRequest,
    *,
    candidates: Sequence[Server | None],
) -> policy.CertificationPolicyResult:
    if not request.model_profile_id:
        return policy.CertificationPolicyResult(allowed=True)
    profile = session.get(ModelProfile, request.model_profile_id)
    if profile is None:
        return policy.CertificationPolicyResult(allowed=True)
    certification = _certification_snapshot(profile)
    if certification is None or certification.enforcement != "certified":
        return policy.evaluate_model_profile_certification(
            certification,
            control_build=policy.BuildProvenanceSnapshot(),
            candidates=[],
        )
    return policy.evaluate_model_profile_certification(
        certification,
        control_build=_build_snapshot(),
        candidates=[
            _candidate_snapshot(
                server,
                profile_id=profile.id,
            )
            for server in candidates
        ],
    )


def require_job_model_profile_certification(
    session: Session,
    request: JobCreateRequest,
    *,
    candidates: Sequence[Server | None],
) -> None:
    policy.raise_for_certification_result(
        evaluate_job_model_profile_certification(
            session,
            request,
            candidates=candidates,
        )
    )


__all__ = [
    "evaluate_job_model_profile_certification",
    "require_job_model_profile_certification",
]
