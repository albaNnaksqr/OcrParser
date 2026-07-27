"""Strict, secret-free engine provenance exchanged by Agent and Control."""

from __future__ import annotations

import ipaddress
import json
import re
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator


MAX_ENGINE_PROVENANCE_FILE_BYTES = 1024 * 1024
ENGINE_PROVENANCE_INVALID = "engine_provenance_invalid"
ENGINE_PROVENANCE_FILE_UNAVAILABLE = "engine_provenance_file_unavailable"
ENGINE_PROVENANCE_FILE_TOO_LARGE = "engine_provenance_file_too_large"

_PROFILE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}$")
_REVISION_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@+-]{0,254}$")
_DIGEST_PATTERN = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_SECRET_MARKERS = (
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "client_secret",
    "credential",
    "password",
    "secret",
    "token",
)
_CUSTOMER_MARKERS = ("customer", "tenant")
_PRIVATE_NAME_SUFFIXES = (
    ".corp",
    ".internal",
    ".intra",
    ".lan",
    ".local",
    ".private",
    ".svc",
)


class EngineProvenanceError(ValueError):
    """Safe provenance validation error whose message never contains input."""


def _safe_identifier(value: str, *, pattern: re.Pattern[str]) -> str:
    if not pattern.fullmatch(value):
        raise ValueError("unsafe identifier")
    lowered = value.lower()
    if any(marker in lowered for marker in _SECRET_MARKERS):
        raise ValueError("secret-like identifier")
    if any(marker in lowered for marker in _CUSTOMER_MARKERS):
        raise ValueError("customer-like identifier")
    if lowered == "localhost" or lowered.endswith(_PRIVATE_NAME_SUFFIXES):
        raise ValueError("address-like identifier")
    try:
        ipaddress.ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("address-like identifier")
    if "." in value and re.fullmatch(
        r"v?\d+(?:\.\d+){1,3}(?:[-+][A-Za-z0-9.-]+)?",
        value,
    ) is None:
        final_segment = value.rsplit(".", 1)[-1]
        if final_segment.isalpha() and len(final_segment) >= 2:
            raise ValueError("address-like identifier")
    return value


class EngineProfileProvenance(BaseModel):
    """Whitelisted provenance for one configured engine profile."""

    model_config = ConfigDict(extra="forbid", strict=True)

    model_revision: str | None = None
    model_digest: str | None = None
    runtime_revision: str | None = None
    runtime_digest: str | None = None
    layout_revision: str | None = None
    layout_digest: str | None = None

    @field_validator("model_revision", "runtime_revision", "layout_revision")
    @classmethod
    def _validate_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_identifier(value, pattern=_REVISION_PATTERN)

    @field_validator("model_digest", "runtime_digest", "layout_digest")
    @classmethod
    def _validate_digest(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not _DIGEST_PATTERN.fullmatch(value):
            raise ValueError("invalid digest")
        return value.lower()


class EngineProvenanceFile(BaseModel):
    """On-disk schema. Build provenance is never accepted from this file."""

    model_config = ConfigDict(extra="forbid", strict=True)

    profiles: dict[str, EngineProfileProvenance]

    @field_validator("profiles")
    @classmethod
    def _validate_profile_ids(
        cls,
        value: dict[str, EngineProfileProvenance],
    ) -> dict[str, EngineProfileProvenance]:
        for profile_id in value:
            _safe_identifier(profile_id, pattern=_PROFILE_ID_PATTERN)
        return value


class EngineProvenanceCapability(EngineProvenanceFile):
    """Wire schema stored inside ``Server.capabilities``."""

    source_revision: str | None = None
    dirty: bool | None = None

    @field_validator("source_revision")
    @classmethod
    def _validate_source_revision(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _safe_identifier(value, pattern=_REVISION_PATTERN)


def _validate_file_payload(payload: Any) -> dict[str, object]:
    try:
        model = EngineProvenanceFile.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        raise EngineProvenanceError(ENGINE_PROVENANCE_INVALID) from None
    return model.model_dump(exclude_none=True)


def sanitize_engine_provenance(payload: Any) -> dict[str, object]:
    """Return a normalized wire payload or raise a fixed, content-free error."""

    try:
        model = EngineProvenanceCapability.model_validate(payload)
    except (TypeError, ValueError, ValidationError):
        raise EngineProvenanceError(ENGINE_PROVENANCE_INVALID) from None
    return model.model_dump(exclude_none=True)


def sanitize_capabilities(capabilities: dict[str, Any]) -> dict[str, Any]:
    """Copy capabilities and fail closed for absent or invalid provenance."""

    sanitized = dict(capabilities)
    raw_provenance = sanitized.get("engine_provenance", {"profiles": {}})
    sanitized["engine_provenance"] = sanitize_engine_provenance(raw_provenance)
    return sanitized


def load_engine_provenance_file(file_path: str | Path) -> dict[str, object]:
    """Load at most one MiB without leaking the path or document contents."""

    try:
        path = Path(file_path)
        with path.open("rb") as handle:
            raw = handle.read(MAX_ENGINE_PROVENANCE_FILE_BYTES + 1)
    except OSError:
        raise EngineProvenanceError(ENGINE_PROVENANCE_FILE_UNAVAILABLE) from None
    if len(raw) > MAX_ENGINE_PROVENANCE_FILE_BYTES:
        raise EngineProvenanceError(ENGINE_PROVENANCE_FILE_TOO_LARGE)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise EngineProvenanceError(ENGINE_PROVENANCE_INVALID) from None
    return _validate_file_payload(payload)


def build_engine_provenance_capability(
    profiles: dict[str, object] | None = None,
) -> dict[str, object]:
    """Add immutable wheel build fields to a prevalidated profile mapping."""

    from ocr_platform.legal import build_provenance

    capability: dict[str, object] = {"profiles": profiles or {}}
    build = build_provenance()
    source_revision = build.get("source_revision")
    dirty = build.get("dirty")
    if isinstance(source_revision, str):
        capability["source_revision"] = source_revision
    if type(dirty) is bool:
        capability["dirty"] = dirty
    try:
        return sanitize_engine_provenance(capability)
    except EngineProvenanceError:
        # Invalid build metadata is omitted rather than replaced by environment
        # or source-offer fallbacks. Profile provenance remains useful.
        return sanitize_engine_provenance({"profiles": profiles or {}})
