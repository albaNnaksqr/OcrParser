from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timezone
from typing import Any

from ...models import ModelProfileCertification
from ...schemas import (
    ModelProfileCertificationRequest,
    ModelProfileCertificationResponse,
    RiskAcceptanceResponse,
)


REVISION_FIELDS = (
    "parser_revision",
    "model_revision",
    "runtime_revision",
    "layout_revision",
)
DIGEST_FIELDS = (
    "parser_digest",
    "model_digest",
    "runtime_digest",
    "layout_digest",
    "fixture_set_digest",
    "evidence_digest",
)
CERTIFIED_REQUIRED_FIELDS = (
    "parser_revision",
    "model_revision",
    "runtime_digest",
    "fixture_set_digest",
    "evidence_digest",
)

_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+-]{0,254}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_UNSAFE_TEXT_PATTERNS = (
    re.compile(r"\b(?:https?|s3)://", re.IGNORECASE),
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}(?::\d+)?\b"),
    re.compile(
        r"\b[a-z0-9-]+(?:\.[a-z0-9-]+)*"
        r"\.(?:internal|corp|local|lan|svc|private|intra)(?::\d+)?\b",
        re.IGNORECASE,
    ),
    re.compile(r"\blocalhost(?::[0-9]{1,5})?\b", re.IGNORECASE),
    re.compile(
        r"(?<![A-Za-z0-9.-])"
        r"(?=[A-Za-z0-9.-]*[A-Za-z])"
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,62})"
        r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,62}))*"
        r"(?::[0-9]{1,5})(?![0-9])"
    ),
    re.compile(
        r"(?<![A-Za-z0-9~])/[A-Za-z0-9._-]+"
        r"(?=/|$|[\s,;)\]}])"
    ),
    re.compile(r"(?<!\w)~/\S+"),
    re.compile(r"\\\\[^\\\s]+\\[^\\\s]+"),
    re.compile(r"\b[A-Za-z]:[\\/]\S+"),
    re.compile(
        r"\b(?:api[_-]?key|authorization|password|secret|token)"
        r"\b\s*[:=]\s*\S+",
        re.IGNORECASE,
    ),
    re.compile(r"\bbearer\s+\S+", re.IGNORECASE),
    re.compile(
        r"\b(?:sk-|gh[opusr]_?|hf_|xox[baprs]-)[A-Za-z0-9_-]{8,}\b",
        re.IGNORECASE,
    ),
)
_IPV6_CANDIDATE_PATTERN = re.compile(
    r"(?<![0-9A-Fa-f:])"
    r"(?:\[[0-9A-Fa-f:]+\](?::\d+)?|[0-9A-Fa-f:]*:[0-9A-Fa-f:]+)"
    r"(?![0-9A-Fa-f:])"
)


def default_certification_response() -> ModelProfileCertificationResponse:
    return ModelProfileCertificationResponse(
        enforcement="off",
        status="contract_only",
    )


def _normalized_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    return normalized or None


def _contains_ipv6_address(value: str) -> bool:
    for match in _IPV6_CANDIDATE_PATTERN.finditer(value):
        candidate = match.group(0)
        if candidate.startswith("["):
            candidate = candidate[1 : candidate.index("]")]
        try:
            if ipaddress.ip_address(candidate).version == 6:
                return True
        except ValueError:
            continue
    return False


def _contains_unsafe_text(value: str) -> bool:
    return _contains_ipv6_address(value) or any(
        pattern.search(value) for pattern in _UNSAFE_TEXT_PATTERNS
    )


def _reject_unsafe_text(value: str, *, field: str) -> None:
    if "\n" in value or "\r" in value:
        raise ValueError(f"certification {field} must be a single line")
    if _contains_unsafe_text(value):
        raise ValueError(
            f"certification {field} may not contain secrets, endpoints, "
            "addresses, or private paths"
        )


def certification_request_values(
    request: ModelProfileCertificationRequest,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "enforcement": request.enforcement,
        "status": request.status,
        "certified_at": request.certified_at,
    }
    for field in REVISION_FIELDS:
        value = _normalized_optional(getattr(request, field))
        if value is not None and not _REVISION_PATTERN.fullmatch(value):
            raise ValueError(
                f"certification {field} must be a revision token, not an "
                "endpoint or path"
            )
        if value is not None:
            _reject_unsafe_text(value, field=field)
        values[field] = value
    for field in DIGEST_FIELDS:
        value = _normalized_optional(getattr(request, field))
        if value is not None and not _DIGEST_PATTERN.fullmatch(value):
            raise ValueError(
                f"certification {field} must use sha256:<64 hex>"
            )
        if value is not None:
            value = value.lower()
        values[field] = value

    if request.status == "certified":
        missing = [
            field for field in CERTIFIED_REQUIRED_FIELDS if not values[field]
        ]
        if missing:
            raise ValueError(
                "certified model profile requires: " + ", ".join(missing)
            )

    risk_acceptance = request.risk_acceptance
    if (
        request.status == "verified"
        and request.enforcement != "off"
        and risk_acceptance is None
    ):
        raise ValueError(
            "verified model profile with enforcement enabled requires "
            "risk_acceptance"
        )
    if risk_acceptance is None:
        values["risk_acceptance_json"] = "{}"
    else:
        _reject_unsafe_text(
            risk_acceptance.accepted_by,
            field="risk_acceptance.accepted_by",
        )
        _reject_unsafe_text(
            risk_acceptance.reason,
            field="risk_acceptance.reason",
        )
        values["risk_acceptance_json"] = json.dumps(
            {
                "accepted_by": risk_acceptance.accepted_by,
                "accepted_at": risk_acceptance.accepted_at.astimezone(
                    timezone.utc
                ).isoformat(),
                "reason": risk_acceptance.reason,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    return values


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _safe_revision(value: str | None) -> str | None:
    value = _normalized_optional(value)
    if value is None or not _REVISION_PATTERN.fullmatch(value):
        return None
    if _contains_unsafe_text(value):
        return None
    return value


def _safe_digest(value: str | None) -> str | None:
    value = _normalized_optional(value)
    if value is None or not _DIGEST_PATTERN.fullmatch(value):
        return None
    return value.lower()


def _risk_acceptance_response(
    payload: str,
) -> RiskAcceptanceResponse | None:
    try:
        value = json.loads(payload or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    allowed = {"accepted_by", "accepted_at", "reason"}
    if set(value) != allowed:
        return None
    try:
        response = RiskAcceptanceResponse.model_validate(value)
        _reject_unsafe_text(
            response.accepted_by,
            field="risk_acceptance.accepted_by",
        )
        _reject_unsafe_text(
            response.reason,
            field="risk_acceptance.reason",
        )
    except (TypeError, ValueError):
        return None
    return response


def certification_to_response(
    certification: ModelProfileCertification | None,
) -> ModelProfileCertificationResponse:
    if certification is None:
        return default_certification_response()
    return ModelProfileCertificationResponse(
        enforcement=certification.enforcement,
        status=certification.status,
        parser_revision=_safe_revision(certification.parser_revision),
        parser_digest=_safe_digest(certification.parser_digest),
        model_revision=_safe_revision(certification.model_revision),
        model_digest=_safe_digest(certification.model_digest),
        runtime_revision=_safe_revision(certification.runtime_revision),
        runtime_digest=_safe_digest(certification.runtime_digest),
        layout_revision=_safe_revision(certification.layout_revision),
        layout_digest=_safe_digest(certification.layout_digest),
        fixture_set_digest=_safe_digest(
            certification.fixture_set_digest
        ),
        evidence_digest=_safe_digest(certification.evidence_digest),
        certified_at=_aware(certification.certified_at),
        risk_acceptance=_risk_acceptance_response(
            certification.risk_acceptance_json
        ),
        updated_at=_aware(certification.updated_at),
    )


__all__ = [
    "CERTIFIED_REQUIRED_FIELDS",
    "DIGEST_FIELDS",
    "REVISION_FIELDS",
    "certification_request_values",
    "certification_to_response",
    "default_certification_response",
]
