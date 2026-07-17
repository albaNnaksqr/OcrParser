from __future__ import annotations

import os

from sqlalchemy.orm import Session

from ...models import Job, ModelProfile
from ...schemas import JobFileResponse, JobResponse
from ..common import json_loads_list, json_loads_object
from ..manifests.core import has_static_shards
from ..model_profiles.core import _resolve_model_profile_api_key
from ..workers.core import public_assigned_server_id


def job_to_response(job: Job, session: Session, include_secrets: bool = False) -> JobResponse:
    extra_args = json_loads_object(job.extra_args_json)
    if include_secrets:
        api_key_env_var = extra_args.pop("api_key_env_var", None)
        if api_key_env_var and "api_key" not in extra_args:
            api_key_from_env = os.environ.get(str(api_key_env_var))
            if api_key_from_env:
                extra_args["api_key"] = api_key_from_env
        if job.model_profile_id and "api_key" not in extra_args:
            profile = session.get(ModelProfile, job.model_profile_id)
            profile_api_key = _resolve_model_profile_api_key(profile) if profile is not None else None
            if profile_api_key:
                extra_args["api_key"] = profile_api_key
    else:
        extra_args.pop("api_key", None)
    return JobResponse(
        id=job.id,
        input_dir=job.input_dir,
        output_dir=job.output_dir,
        engine=job.engine,
        model_profile_id=job.model_profile_id,
        input_mode=job.input_mode,
        manifest_root=job.manifest_root,
        target_files_per_shard=job.target_files_per_shard,
        max_shard_attempts=job.max_shard_attempts,
        assigned_server_id=public_assigned_server_id(job),
        allowed_server_ids=json_loads_list(job.allowed_server_ids_json),
        status=job.status,
        failure_category=job.failure_category,
        error_message=job.error_message,
        stop_requested=job.stop_requested,
        force_reprocess=job.force_reprocess,
        archived_at=job.archived_at,
        engine_config=job.engine_config,
        ip=job.ip,
        port=job.port,
        model_name=job.model_name,
        page_concurrency=job.page_concurrency,
        has_static_shards=has_static_shards(session, job.id),
        extra_args=extra_args,
        command=json_loads_list(job.command_json),
        files=[job_file_to_response(item) for item in job.files],
    )


def job_file_to_response(item) -> JobFileResponse:
    return JobFileResponse(
        file_path=item.file_path,
        filename=item.filename,
        status=item.status,
        total_pages=item.total_pages,
        done_pages=item.done_pages,
        output_path=item.output_path,
        error=item.error,
        failure_category=item.failure_category,
    )
