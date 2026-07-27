from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


DEFAULT_JOB_FILE_DETAIL_LIMIT = 10_000
DEFAULT_JOB_EVENT_DETAIL_LIMIT = 50_000
DEFAULT_JOB_LOG_DETAIL_LIMIT = 10_000
DEFAULT_JOB_FAILED_FILE_SAMPLE_LIMIT = 100
DEFAULT_JOB_SUMMARY_ATTENTION_SHARD_LIMIT = 50
DEFAULT_RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED = 1


def _non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= 0 else default


def _environment_limit(
    environment: Mapping[str, str],
    name: str,
    default: int,
) -> int:
    return _non_negative_int(environment.get(name), default)


def _legacy_int(value: Any, default: int) -> int:
    return (
        value
        if isinstance(value, int) and not isinstance(value, bool)
        else default
    )


@dataclass(frozen=True, slots=True)
class ControlLimits:
    job_file_detail_limit: int = DEFAULT_JOB_FILE_DETAIL_LIMIT
    job_event_detail_limit: int = DEFAULT_JOB_EVENT_DETAIL_LIMIT
    job_log_detail_limit: int = DEFAULT_JOB_LOG_DETAIL_LIMIT
    job_failed_file_sample_limit: int = (
        DEFAULT_JOB_FAILED_FILE_SAMPLE_LIMIT
    )
    job_recent_error_sample_limit: int = (
        DEFAULT_JOB_FAILED_FILE_SAMPLE_LIMIT
    )
    job_summary_attention_shard_limit: int = (
        DEFAULT_JOB_SUMMARY_ATTENTION_SHARD_LIMIT
    )
    retained_control_event_limit_when_details_disabled: int = (
        DEFAULT_RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED
    )

    @property
    def persist_job_file_details(self) -> bool:
        return self.job_file_detail_limit != 0

    @property
    def persist_job_event_details(self) -> bool:
        return self.job_event_detail_limit != 0

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "ControlLimits":
        values = os.environ if environment is None else environment
        failed_sample_limit = _environment_limit(
            values,
            "OCR_JOB_FAILED_FILE_SAMPLE_LIMIT",
            DEFAULT_JOB_FAILED_FILE_SAMPLE_LIMIT,
        )
        return cls(
            job_file_detail_limit=_environment_limit(
                values,
                "OCR_JOB_FILE_DETAIL_LIMIT",
                DEFAULT_JOB_FILE_DETAIL_LIMIT,
            ),
            job_event_detail_limit=_environment_limit(
                values,
                "OCR_JOB_EVENT_DETAIL_LIMIT",
                DEFAULT_JOB_EVENT_DETAIL_LIMIT,
            ),
            job_log_detail_limit=_environment_limit(
                values,
                "OCR_JOB_LOG_DETAIL_LIMIT",
                DEFAULT_JOB_LOG_DETAIL_LIMIT,
            ),
            job_failed_file_sample_limit=failed_sample_limit,
            job_recent_error_sample_limit=_environment_limit(
                values,
                "OCR_JOB_RECENT_ERROR_SAMPLE_LIMIT",
                failed_sample_limit,
            ),
            job_summary_attention_shard_limit=_environment_limit(
                values,
                "OCR_JOB_SUMMARY_ATTENTION_SHARD_LIMIT",
                DEFAULT_JOB_SUMMARY_ATTENTION_SHARD_LIMIT,
            ),
            retained_control_event_limit_when_details_disabled=(
                DEFAULT_RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED
            ),
        )


_IMPORTED_LIMITS = ControlLimits.from_environment()
JOB_FILE_DETAIL_LIMIT = _IMPORTED_LIMITS.job_file_detail_limit
JOB_EVENT_DETAIL_LIMIT = _IMPORTED_LIMITS.job_event_detail_limit
JOB_LOG_DETAIL_LIMIT = _IMPORTED_LIMITS.job_log_detail_limit
JOB_FAILED_FILE_SAMPLE_LIMIT = _IMPORTED_LIMITS.job_failed_file_sample_limit
JOB_RECENT_ERROR_SAMPLE_LIMIT = _IMPORTED_LIMITS.job_recent_error_sample_limit
JOB_SUMMARY_ATTENTION_SHARD_LIMIT = (
    _IMPORTED_LIMITS.job_summary_attention_shard_limit
)
RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED = (
    _IMPORTED_LIMITS.retained_control_event_limit_when_details_disabled
)


def legacy_control_limits() -> ControlLimits:
    failed_sample_limit = _legacy_int(
        JOB_FAILED_FILE_SAMPLE_LIMIT,
        DEFAULT_JOB_FAILED_FILE_SAMPLE_LIMIT,
    )
    return ControlLimits(
        job_file_detail_limit=_legacy_int(
            JOB_FILE_DETAIL_LIMIT,
            DEFAULT_JOB_FILE_DETAIL_LIMIT,
        ),
        job_event_detail_limit=_legacy_int(
            JOB_EVENT_DETAIL_LIMIT,
            DEFAULT_JOB_EVENT_DETAIL_LIMIT,
        ),
        job_log_detail_limit=_legacy_int(
            JOB_LOG_DETAIL_LIMIT,
            DEFAULT_JOB_LOG_DETAIL_LIMIT,
        ),
        job_failed_file_sample_limit=failed_sample_limit,
        job_recent_error_sample_limit=_legacy_int(
            JOB_RECENT_ERROR_SAMPLE_LIMIT,
            failed_sample_limit,
        ),
        job_summary_attention_shard_limit=_legacy_int(
            JOB_SUMMARY_ATTENTION_SHARD_LIMIT,
            DEFAULT_JOB_SUMMARY_ATTENTION_SHARD_LIMIT,
        ),
        retained_control_event_limit_when_details_disabled=(
            _legacy_int(
                RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED,
                DEFAULT_RETAINED_CONTROL_EVENT_LIMIT_WHEN_DETAILS_DISABLED,
            )
        ),
    )


__all__ = [
    "ControlLimits",
    "legacy_control_limits",
]
