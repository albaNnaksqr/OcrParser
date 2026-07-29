"""Worker availability and deployment preflight."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from ... import certification_gate, database
from ...limits import ControlLimits, legacy_control_limits
from ...models import ModelProfile
from ...schemas import (
    JobCreateRequest,
    JobPreflightIssue,
    JobPreflightResponse,
)
from ...settings import ControlSettings
from ..common import POOL_SERVER_ID
from ..manifests.paths import infer_default_manifest_root
from ..model_profiles.queries import resolve_model_profile_api_key
from .eligibility import (
    candidate_workers_for_job,
    evaluate_server_path_access,
    list_server_eligibility,
)
from .projection import (
    list_servers,
    resource_constrained_workers,
    server_versions,
    workers_with_event_spool_backlog,
    workers_with_pending_shard_update_backlog,
)


def preflight_issue(
    severity: str,
    code: str,
    message: str,
    **details: Any,
) -> JobPreflightIssue:
    return JobPreflightIssue(
        severity=severity,
        code=code,
        message=message,
        details=details,
    )


def database_migration_preflight_issue(
    database_status: dict[str, Any],
) -> JobPreflightIssue | None:
    dialect = str(database_status.get("dialect") or "")
    if dialect != "postgresql":
        return None

    known_migrations = [
        str(item)
        for item in database_status.get("known_migrations") or []
    ]
    latest_known_migration = (
        known_migrations[-1] if known_migrations else None
    )
    if not database_status.get("schema_migrations_table_exists"):
        return preflight_issue(
            "error",
            "database_migrations_missing",
            "PostgreSQL control database is missing schema_migrations; "
            "apply the SQL migration baseline before creating production "
            "jobs.",
            dialect=dialect,
            known_migrations=known_migrations,
            latest_known_migration=latest_known_migration,
        )

    applied_versions: set[str] = set()
    for item in database_status.get("applied_migrations") or []:
        if isinstance(item, dict) and item.get("version"):
            applied_versions.add(str(item["version"]))
        elif item:
            applied_versions.add(str(item))
    missing_migrations = [
        version
        for version in known_migrations
        if version not in applied_versions
    ]
    if missing_migrations:
        return preflight_issue(
            "error",
            "database_migration_not_current",
            "PostgreSQL control database has unapplied SQL migrations; "
            "apply migrations before creating production jobs.",
            dialect=dialect,
            known_migrations=known_migrations,
            missing_migrations=missing_migrations,
            latest_known_migration=latest_known_migration,
            latest_applied_migration=database_status.get(
                "latest_applied_migration"
            ),
        )

    checksum_mismatches = (
        database_status.get("checksum_mismatches") or []
    )
    missing_checksums = database_status.get("missing_checksums") or []
    unexpected_migrations = (
        database_status.get("unexpected_migrations") or []
    )
    if (
        checksum_mismatches
        or missing_checksums
        or unexpected_migrations
    ):
        return preflight_issue(
            "error",
            "database_migration_verification_failed",
            "PostgreSQL control database migration history failed "
            "checksum verification.",
            dialect=dialect,
            checksum_mismatches=checksum_mismatches,
            missing_checksums=missing_checksums,
            unexpected_migrations=unexpected_migrations,
        )

    return None


def control_api_auth_preflight_issue(
    settings: ControlSettings | None = None,
) -> JobPreflightIssue | None:
    control_settings = (
        settings
        if settings is not None
        else ControlSettings.from_environment()
    )
    if control_settings.api_token:
        return None
    return preflight_issue(
        "warning",
        "control_api_auth_disabled",
        "Control API token authentication is not configured; set "
        "OCR_PLATFORM_API_TOKEN before exposing production endpoints.",
        require_api_token=control_settings.require_api_token,
    )


def preflight_job(
    session: Session,
    request: JobCreateRequest,
    *,
    settings: ControlSettings | None = None,
    limits: ControlLimits | None = None,
) -> JobPreflightResponse:
    control_settings = (
        settings
        if settings is not None
        else ControlSettings.from_environment()
    )
    control_limits = (
        limits if limits is not None else legacy_control_limits()
    )
    issues: list[JobPreflightIssue] = []
    database_status = database.describe_database_status(
        session.get_bind()
    )
    database_dialect = str(
        database_status.get("dialect")
        or session.get_bind().dialect.name
    )
    if database_dialect != "postgresql":
        issues.append(
            preflight_issue(
                "warning",
                "database_not_postgres",
                "Production jobs should use PostgreSQL; SQLite is for "
                "local development.",
                dialect=database_dialect,
                require_postgres=control_settings.require_postgres,
            )
        )
    migration_issue = database_migration_preflight_issue(
        database_status
    )
    if migration_issue is not None:
        issues.append(migration_issue)
    auth_issue = control_api_auth_preflight_issue(control_settings)
    if auth_issue is not None:
        issues.append(auth_issue)

    allowed_ids = set(request.allowed_server_ids or [])
    if request.assigned_server_id:
        allowed_ids.add(request.assigned_server_id)
    eligibilities = [
        item
        for item in list_server_eligibility(
            session,
            request.input_dir,
        )
        if item["server_id"] != POOL_SERVER_ID
        and (
            not allowed_ids
            or item["server_id"] in allowed_ids
        )
    ]
    eligible = [
        item for item in eligibilities if item.get("can_access")
    ]
    ready = [
        item
        for item in eligible
        if item.get("status") in {"online", "idle"}
        and not item.get("is_stale")
    ]
    if not eligible:
        issues.append(
            preflight_issue(
                "error",
                "no_eligible_workers",
                "No selected worker can read the input shared path.",
                input_dir=request.input_dir,
            )
        )
    eligible_ids = {
        str(item["server_id"]) for item in eligible
    }

    def writable_workers_for(
        path: str,
    ) -> list[dict[str, Any]]:
        checks = [
            evaluate_server_path_access(
                server,
                path,
                require_writable=True,
            )
            for server in list_servers(session)
            if server.id in eligible_ids
        ]
        return [
            item for item in checks if item.get("can_access")
        ]

    output_writers = {
        str(item["server_id"])
        for item in writable_workers_for(request.output_dir)
    }
    missing_output_writers = sorted(
        eligible_ids - output_writers
    )
    if missing_output_writers:
        issues.append(
            preflight_issue(
                "error",
                "output_path_not_writable",
                "One or more eligible workers cannot confirm write "
                "access to output_dir.",
                path=request.output_dir,
                eligible_workers=sorted(eligible_ids),
                writable_workers=sorted(output_writers),
                unwritable_workers=missing_output_writers,
            )
        )
    effective_manifest_root = (
        request.manifest_root
        or infer_default_manifest_root(
            session,
            input_dir=request.input_dir,
            input_mode=request.input_mode,
            assigned_server_id=request.assigned_server_id,
            allowed_server_ids=request.allowed_server_ids,
        )
    )
    if effective_manifest_root:
        manifest_writers = {
            str(item["server_id"])
            for item in writable_workers_for(
                effective_manifest_root
            )
        }
        missing_manifest_writers = sorted(
            eligible_ids - manifest_writers
        )
        if missing_manifest_writers:
            issues.append(
                preflight_issue(
                    "error",
                    "manifest_root_not_writable",
                    "One or more eligible workers cannot confirm write "
                    "access to manifest_root.",
                    path=effective_manifest_root,
                    inferred=not bool(request.manifest_root),
                    eligible_workers=sorted(eligible_ids),
                    writable_workers=sorted(manifest_writers),
                    unwritable_workers=missing_manifest_writers,
                )
            )
    versions = server_versions(
        session,
        {
            str(item["server_id"])
            for item in eligible
        },
    )
    if len(versions) > 1:
        issues.append(
            preflight_issue(
                "warning",
                "mixed_worker_versions",
                "Selected eligible workers report different git_ref "
                "or script_version values.",
                versions=versions,
            )
        )
    constrained_workers = resource_constrained_workers(
        session,
        eligible_ids,
    )
    if constrained_workers:
        issues.append(
            preflight_issue(
                "warning",
                "resource_constrained_workers",
                "One or more eligible workers currently report "
                "resource pressure and may delay claiming work.",
                workers=constrained_workers,
            )
        )
    backlog_workers = workers_with_event_spool_backlog(
        session,
        eligible_ids,
    )
    if backlog_workers:
        issues.append(
            preflight_issue(
                "warning",
                "worker_event_spool_backlog",
                "One or more eligible workers report unreplayed, "
                "quarantined, or dropped local event/log spool records.",
                workers=backlog_workers,
            )
        )
    pending_update_workers = (
        workers_with_pending_shard_update_backlog(
            session,
            eligible_ids,
        )
    )
    if pending_update_workers:
        issues.append(
            preflight_issue(
                "warning",
                "worker_pending_shard_update_backlog",
                "One or more eligible workers report unreplayed or "
                "quarantined local shard progress updates.",
                workers=pending_update_workers,
            )
        )

    if request.model_profile_id:
        profile = session.get(
            ModelProfile,
            request.model_profile_id,
        )
        if profile is None:
            issues.append(
                preflight_issue(
                    "error",
                    "unknown_model_profile",
                    "Selected model profile does not exist.",
                    model_profile_id=request.model_profile_id,
                )
            )
        elif profile.requires_api_key and not (
            resolve_model_profile_api_key(profile)
            or request.extra_args.get("api_key")
        ):
            issues.append(
                preflight_issue(
                    "error",
                    "model_profile_missing_api_key",
                    "Selected model profile requires an API key, but no "
                    "saved or per-job key is available.",
                    model_profile_id=request.model_profile_id,
                )
            )
        elif profile.api_key:
            issues.append(
                preflight_issue(
                    "warning",
                    "model_profile_saved_api_key",
                    "Selected model profile stores a legacy API key in "
                    "the control database; save the profile with "
                    "clear_api_key=true and migrate to api_key_env_var.",
                    model_profile_id=request.model_profile_id,
                    api_key_env_var=profile.api_key_env_var,
                )
            )
        certification_result = (
            certification_gate
            .evaluate_job_model_profile_certification(
                session,
                request,
                candidates=candidate_workers_for_job(
                    session,
                    request,
                ),
            )
        )
        if not certification_result.allowed:
            issues.append(
                preflight_issue(
                    "error",
                    str(certification_result.code),
                    str(certification_result.message),
                    **certification_result.details,
                )
            )

    if (
        control_limits.job_file_detail_limit > 100000
        or control_limits.job_event_detail_limit > 100000
    ):
        issues.append(
            preflight_issue(
                "warning",
                "high_detail_row_limits",
                "Large per-file or raw-event retention limits can grow "
                "quickly on million-scale jobs.",
                job_file_detail_limit=(
                    control_limits.job_file_detail_limit
                ),
                job_event_detail_limit=(
                    control_limits.job_event_detail_limit
                ),
            )
        )

    return JobPreflightResponse(
        ok=not any(
            issue.severity == "error" for issue in issues
        ),
        database_dialect=database_dialect,
        total_workers=len(eligibilities),
        eligible_workers=len(eligible),
        ready_workers=len(ready),
        issues=issues,
    )


_preflight_issue = preflight_issue
_database_migration_preflight_issue = (
    database_migration_preflight_issue
)
_control_api_auth_preflight_issue = (
    control_api_auth_preflight_issue
)

__all__ = [
    "_control_api_auth_preflight_issue",
    "_database_migration_preflight_issue",
    "_preflight_issue",
    "control_api_auth_preflight_issue",
    "database_migration_preflight_issue",
    "preflight_issue",
    "preflight_job",
]
