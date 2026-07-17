from pathlib import Path


UI_FILE = Path(__file__).resolve().parents[1] / "ocr_platform" / "control" / "ui" / "index.html"


def test_ui_exposes_worker_readiness_and_preflight_diagnostics():
    html = UI_FILE.read_text(encoding="utf-8")

    assert "Worker Readiness" in html
    assert "Ready workers" in html
    assert "Warning workers" in html
    assert "Blocked workers" in html
    assert "classifyWorkerReadiness" in html
    assert "renderPreflightDiagnosis" in html
    assert "resource_constrained_workers" in html
    assert "database_migrations_missing" in html
    assert "database_migration_not_current" in html
    assert "control_api_auth_disabled" in html
    assert "control API auth is disabled" in html
    assert "archiveServer" in html
    assert "removeServerBtn" in html


def test_ui_exposes_deployment_doctor_as_first_class_panel():
    html = UI_FILE.read_text(encoding="utf-8")

    assert "System Summary" in html
    assert "Deployment Doctor" in html
    assert 'id="deploymentDoctorStatus"' in html
    assert 'id="refreshDeploymentDoctorBtn"' in html
    assert 'id="adminOperationsPanel"' in html
    assert "loadDeploymentDoctor" in html
    assert "renderDeploymentDoctor" in html
    assert "/api/system/diagnostics" in html
    assert "/readyz" in html
    assert "database_not_postgres" in html
    assert "api_auth_disabled" in html
    assert "no_ready_workers" in html
    assert "Ready to submit UI jobs" in html


def test_ui_uses_operations_first_layout_for_production_monitoring():
    html = UI_FILE.read_text(encoding="utf-8")

    assert 'class="ops-shell"' in html
    assert 'class="ops-sidebar"' in html
    assert 'class="ops-main"' in html
    assert 'id="opsQueueSummary"' in html
    assert 'id="opsWorkerSummary"' in html
    assert 'id="newJobPanel"' in html
    assert 'id="adminOperationsPanel"' in html
    assert "Live work queue" in html
    assert "Needs attention" in html
    assert html.index("Live work queue") < html.index('id="newJobPanel"')
    assert html.index('id="newJobPanel"') < html.index('id="adminOperationsPanel"')


def test_ui_exposes_remote_worker_lifecycle_controls():
    html = UI_FILE.read_text(encoding="utf-8")

    assert "Remote worker lifecycle" in html
    assert 'id="remoteWorkerTarget"' in html
    assert 'id="refreshRemoteWorkerTargetsBtn"' in html
    assert 'id="remoteWorkerHost"' in html
    assert 'id="remoteWorkerPreflightBtn"' in html
    assert 'id="remoteWorkerInstallDryRunBtn"' in html
    assert 'id="remoteWorkerInstallApplyBtn"' in html
    assert 'id="remoteWorkerServiceAction"' in html
    assert "/api/remote-workers/targets" in html
    assert "/api/remote-workers/preflight" in html
    assert "/api/remote-workers/install-dry-run" in html
    assert "/api/remote-workers/install-apply" in html
    assert "/api/remote-workers/service" in html
    assert "applyRemoteWorkerTarget" in html
    assert "manual target" in html
    assert "not an SSH shell" in html
    assert "Remove" in html


def test_ui_exposes_worker_scale_plan_controls_and_structured_results():
    html = UI_FILE.read_text(encoding="utf-8")

    assert "Worker Scale Plan" in html
    assert 'id="workerScalePanel"' in html
    assert 'id="workerScaleTargetCount"' in html
    assert 'id="workerScaleServerIdPrefix"' in html
    assert 'id="previewWorkerScaleBtn"' in html
    assert 'id="applyWorkerScaleBtn"' in html
    assert 'id="workerScaleResult"' in html
    assert "Preview Scale Plan" in html
    assert "Apply Scale" in html
    assert "Waiting for heartbeat confirmation" in html
    assert "/api/remote-workers/scale-plan" in html
    assert "/api/remote-workers/scale-apply" in html
    assert "renderWorkerScaleResult" in html
    assert "waitForWorkerScaleHeartbeat" in html
    assert "plan_items" in html
    assert "[object Object]" not in html


def test_ui_surfaces_worker_event_spool_backlog_in_worker_details():
    html = UI_FILE.read_text(encoding="utf-8")

    assert "formatWorkerEventSpool" in html
    assert "workerEventSpoolBacklog" in html
    assert "Event/log spool" in html
    assert "dropped_events" in html
    assert "Dropped events" in html
    assert "Dropped logs" in html
    assert "pending_events" in html
    assert "pending_logs" in html
    assert "failed_events" in html
    assert "failed_logs" in html
    assert "event/log spool backlog" in html


def test_ui_surfaces_worker_pending_shard_update_backlog_in_worker_details():
    html = UI_FILE.read_text(encoding="utf-8")

    assert "formatWorkerPendingShardUpdates" in html
    assert "workerPendingShardUpdateBacklog" in html
    assert "Shard update spool" in html
    assert "pending_shard_updates" in html
    assert "shard update backlog" in html


def test_submit_job_ui_keeps_model_config_in_profile_and_advanced_minimal():
    html = UI_FILE.read_text(encoding="utf-8")

    assert 'id="resolvedModelConfig"' in html
    assert 'id="numCpuWorkers"' in html
    assert 'id="fileConcurrency"' in html
    assert 'id="apiConcurrencyStart"' in html
    assert 'id="apiConcurrencyMax"' in html
    assert 'id="enableApiAutotune"' in html
    assert 'id="apiAutotuneInterval"' in html
    assert 'id="maxShardAttempts"' in html
    assert 'id="manifestPathField"' in html
    assert 'id="workerScope"' in html
    assert 'id="selectedWorkersPanel"' in html
    assert 'id="singleWorkerSelect"' in html
    assert 'id="selectedWorkersList"' in html
    assert 'type="checkbox"' in html
    assert "renderWorkerSelectors" in html
    assert "syncExecutionModeFields" in html
    assert "selectedAllowedServerIds" in html
    assert "buildModelPayload" in html
    assert "extraArgs.file_concurrency = fileConcurrency" in html
    assert "extraArgs.api_concurrency_start = apiConcurrencyStart" in html
    assert "extraArgs.api_concurrency_max = apiConcurrencyMax" in html
    assert "extraArgs.enable_api_autotune = true" in html
    assert "extraArgs.api_autotune_interval = apiAutotuneInterval" in html
    assert "recovery_status" in html
    assert "retrying_shards" in html
    assert "stale_shards" in html
    assert 'id="ip"' not in html
    assert 'id="port"' not in html
    assert 'id="modelName"' not in html
    assert 'id="engine"' not in html
    assert 'id="extraArgs"' not in html
    assert 'id="forceReprocess"' not in html
    assert 'id="engineConfig"' not in html


def test_dotsocr_is_the_temporary_default_model_profile():
    html = UI_FILE.read_text(encoding="utf-8")

    assert 'const DEFAULT_MODEL_PROFILE = "dotsocr_15";' in html
    assert "DEFAULT_MODEL_PROFILE" in html
    assert "file_concurrency: 8" in html
    assert "api_concurrency_start: 80" in html
    assert "api_concurrency_max: 80" in html


def test_model_profiles_can_be_loaded_and_saved_from_control_api():
    html = UI_FILE.read_text(encoding="utf-8")

    assert 'id="modelProfileEditor"' in html
    assert 'id="profileApiKey" type="text"' in html
    assert 'id="saveModelProfileBtn"' in html
    assert 'id="profileHasApiKey"' in html
    assert 'id="profileApiKeyEnvVar"' in html
    assert "formatProfileKeyStatus" in html
    assert "api_key_env_var" in html
    assert "API key saved for this profile" in html
    assert "API key resolved from env var" in html
    assert "API key saved and available for jobs" in html
    assert "API key cleared" in html
    assert "OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS" in html
    assert "extra_args must not contain secret-like keys" in html
    assert "token, secret, password, authorization, or API key" in html
    assert 'requestJson("/api/model-profiles")' in html
    assert '`/api/model-profiles/${encodeURIComponent(profileId)}`' in html
    assert "model_profile_id: ui.modelProfile.value" in html
    assert "database-saved keys are legacy-only" in html
    assert "Kept in this browser form only" not in html


def test_ui_can_attach_control_api_token_to_requests():
    html = UI_FILE.read_text(encoding="utf-8")

    assert 'id="controlApiToken"' in html
    assert "OCR_PLATFORM_UI_TOKEN" in html
    assert "apiRequestOptions" in html
    assert "tokenRefreshTimer" in html
    assert "refreshOperationsData" in html
    assert "Promise.allSettled" in html
    assert "doctor.workers" in html
    assert "doctorWorkers.ready" in html
    assert "sessionStorage.setItem(API_TOKEN_STORAGE_KEY" in html
    assert "sessionStorage.removeItem(API_TOKEN_STORAGE_KEY" in html
    assert "localStorage.setItem(API_TOKEN_STORAGE_KEY" not in html
    assert 'headers.set("X-API-Key", token)' in html
    assert "fetch(url, apiRequestOptions(options))" in html
    assert "refreshOperationsData({ quiet: !ui.controlApiToken.value.trim() })" in html


def test_ui_displays_agpl_legal_and_corresponding_source_notices():
    html = UI_FILE.read_text(encoding="utf-8")

    assert 'aria-label="Open-source legal notice"' in html
    assert 'href="/source"' in html
    assert 'href="/legal/agpl-3.0"' in html
    assert "without warranty" in html
    assert "You may convey" in html


def test_ui_surfaces_database_migration_status_for_operations():
    html = UI_FILE.read_text(encoding="utf-8")

    assert 'id="databaseStatus"' in html
    assert "loadDatabaseStatus" in html
    assert "/api/system/database" in html
    assert "latest_applied_migration" in html
    assert "schema_migrations_table_exists" in html
    assert "known_migrations" in html
    assert "missing_migrations" in html
    assert "migration currency" in html
    assert "missing migrations" in html


def test_manifest_root_defaults_to_platform_manifest_dir_under_shared_root():
    html = UI_FILE.read_text(encoding="utf-8")

    assert '.ocr_platform/manifests' in html
    assert "inferDefaultManifestRoot" in html
    assert "applyDefaultManifestRoot" in html
    assert "lastAutoManifestRoot" in html


def test_create_job_reuses_preflight_worker_scope_for_shared_path_message():
    html = UI_FILE.read_text(encoding="utf-8")
    start = html.index("async function createJob()")
    end = html.index("async function stopJob", start)
    body = html[start:end]

    assert "const poolMode = payload.assigned_server_id == null;" in body
    assert "const allowedServerIds = Array.isArray(payload.allowed_server_ids)" in body
    assert 'const serverId = payload.assigned_server_id || "";' in body


def test_manifest_integrity_ui_distinguishes_worker_only_paths_from_failed_checks():
    html = UI_FILE.read_text(encoding="utf-8")

    assert 'payload.status === "not_accessible_from_control"' in html
    assert "not accessible" in html
    assert "control process cannot read directly" in html
    assert "Request worker check" in html
    assert "requestWorkerManifestIntegrity" in html
    assert "/manifest/integrity/worker-request" in html
    assert "checked by worker" in html


def test_jobs_ui_surfaces_job_overview_and_attention_shards():
    html = UI_FILE.read_text(encoding="utf-8")

    assert "explainJobStatus" in html
    assert "formatJobStatusExplanation" in html
    assert "formatJobTechnicalState" in html
    assert "Waiting for first update" in html
    assert "Almost done" in html
    assert "Worker has not started real work" in html
    assert "Model API may be stuck" in html
    assert "No recent update" in html
    assert "Waiting for scan worker" in html
    assert "Setup needs attention" in html
    assert "Open Recent errors" in html
    assert "Open logs" in html
    assert "Check Deployment Doctor" in html
    assert 'title="Technical state:' in html
    assert "Refreshes every 3s" in html
    assert "firstStartupStalledShard" in html
    assert "running_seconds" in html
    assert "started_at" in html
    assert "state.deploymentDoctor" in html
    assert "formatJobOverview" in html
    assert "formatAttentionShards" in html
    assert "formatCurrentShard" in html
    assert "scanned_files" in html
    assert "shards_created" in html
    assert "Scanned files" in html
    assert "Shards created" in html
    assert "attention_shards" in html
    assert "current_shards" in html
    assert "processed_files" in html
    assert "completed_pages" in html
    assert "lease_status" in html
    assert "api_inflight" in html
    assert "oldest_api_inflight" in html
    assert "execution_paused" in html
    assert "api_concurrency_limit" in html
    assert "execution_control_reason" in html
    assert "Execution" in html
    assert "API inflight" in html
    assert "Inspect shards" in html
    assert "/shards?" in html
    assert "state.shardInspectors" in html
    assert "Only active, stale, retrying, and failed shards are shown" in html
    assert "Showing first" in html
    assert "attention shards" in html


def test_jobs_progress_does_not_render_unknown_page_total_as_question_mark():
    html = UI_FILE.read_text(encoding="utf-8")
    start = html.index("function rowProgress")
    end = html.index("function rowShards", start)
    body = html[start:end]

    assert '"page" : "pages"' in body
    assert "done`" in body
    assert 'job.total_pages == null ? "?"' not in body
    assert "/${pageTotal} pages" not in body


def test_jobs_ui_exposes_production_scale_controls_and_diagnostics():
    html = UI_FILE.read_text(encoding="utf-8")

    assert 'id="jobStatusFilter"' in html
    assert 'id="jobPageSize"' in html
    assert 'id="previousJobsBtn"' in html
    assert 'id="nextJobsBtn"' in html
    assert "state.jobsPage" in html
    assert "buildJobsSummaryUrl" in html
    assert "/api/jobs/summary/page" in html
    assert "payload.has_more" in html
    assert "payload.total" in html
    assert 'params.set("status", state.jobsPage.status)' in html
    assert "include_archived" in html
    assert "archiveJob" in html
    assert "archiveJobBtn" in html
    assert "/archive" in html
    assert "limit" in html
    assert "offset" in html

    assert "formatScanLifecycle" in html
    assert "lifecycle_stage" in html
    assert ".sharding" in html
    assert "formatJobLifecycle" in html
    assert "Lifecycle" in html
    assert "Scan lifecycle" in html
    assert "scan_status" in html
    assert "scan_progress_files" in html
    assert "scan_discovered_pdf_count" in html
    assert "scan_estimated_total_files" in html
    assert "scan_estimated_total_pdf_count" in html
    assert "scan_remaining_files" in html
    assert "scan_remaining_pdf_count" in html
    assert "Remaining PDFs" in html
    assert "scan_progress_percent" in html
    assert "scan_progress_dirs" in html
    assert "scan_eta_seconds" in html
    assert "scan_started_at" in html
    assert "scan_finished_at" in html
    assert "function formatDateTime" in html
    assert "executable_shards" in html
    assert "scan_error_samples" in html
    assert 'failure_category || "scan_error"' in html
    assert "Scanning" in html
    work_plan_start = html.index("function formatWorkPlan")
    work_plan_end = html.index("function leaseStatusClass")
    work_plan_body = html[work_plan_start:work_plan_end]
    assert "manifest_snapshot_status" in work_plan_body
    assert "manifest_status" in work_plan_body
    assert "manifest_frozen_at" in work_plan_body
    assert "manifest_integrity_status" in work_plan_body
    assert "manifest_integrity_issue_count" in work_plan_body
    assert "Generated shards" in work_plan_body
    assert "Executable shards" in work_plan_body
    assert "Snapshot" in work_plan_body
    assert "Frozen at" in work_plan_body
    assert "Freeze integrity" in work_plan_body

    assert "formatManifestIntegrity" in html
    assert "checkManifestIntegrity" in html
    assert "/manifest/integrity" in html
    assert "Manifest integrity" in html
    assert "manifest_error" in html
    assert "meta_error" in html
    assert "meta_file_count_matches" in html
    assert "meta_expected_file_count" in html
    assert "meta_actual_file_count" in html
    assert "manifest_file_count_matches" in html
    assert "shard_file_count_matches_manifest" in html
    assert "shard_expected_file_count" in html
    assert "shard_reference_file_count" in html
    assert "scan_unit_manifest_count_matches" in html
    assert "bad_scan_units" in html
    assert "bad_scan_unit_count" in html
    assert "bad_shard_count" in html
    assert "sampled" in html
    assert "scan-unit manifest issues" in html
    assert "formatManifestFreezeReport" in html
    assert "checkManifestFreezeReport" in html
    assert "/manifest/freeze-report" in html
    assert "Manifest freeze" in html
    assert "frozen_at" in html

    assert "loadShardAttempts" in html
    assert "formatShardAttempts" in html
    assert "/attempts/page" in html
    assert "Attempt history" in html
    assert "attemptPageBtn" in html
    assert "attemptsPayload.has_more" in html
    assert "Attempt page" in html
    assert "attempt.execution_paused" in html
    assert "attempt.api_concurrency_limit" in html
    assert "attempt.execution_control_reason" in html
    assert "showAttemptsBtn" in html
    assert "shardWorkerFilter" in html
    assert "shardFailureCategoryFilter" in html
    assert "failure_category" in html
    assert "shardMinAttemptsFilter" in html
    assert "shardRunningLongerFilter" in html

    assert 'id="preflightJobBtn"' in html
    assert "runJobPreflight" in html
    assert "/api/jobs/preflight" in html
    assert "Preflight" in html
    assert "model_profile_missing_api_key" in html
    assert "model_profile_saved_api_key" in html
    assert "clear it and migrate to api_key_env_var" in html
    assert "output_path_not_writable" in html
    assert "manifest_root_not_writable" in html
    assert "worker_event_spool_backlog" in html
    assert "eligible workers have local event/log spool backlog" in html
    assert "worker_pending_shard_update_backlog" in html
    assert "eligible workers have local shard update backlog" in html
    assert "worker_id" in html
    assert "min_attempt_count" in html
    assert "running_longer_than_seconds" in html

    assert "formatVersionWarning" in html
    assert "mixed worker versions" in html
    assert "formatJobVersionWarning" in html
    assert "worker_version_status" in html
    assert "worker_version_refs" in html

    assert "loadJobLogs" in html
    assert "formatJobLogs" in html
    assert "/logs/page" in html
    assert "Job logs" in html
    assert "logPageBtn" in html

    assert "loadRecentErrors" in html
    assert "formatRecentErrors" in html
    assert "/recent-errors/page" in html
    assert "Recent errors" in html
    assert "recentErrorPageBtn" in html


def test_jobs_ui_surfaces_failure_category_count_summaries():
    html = UI_FILE.read_text(encoding="utf-8")

    assert "formatFailureCategoryCounts" in html
    assert "failure_category_counts" in html
    assert "shard_failure_category_counts" in html
    assert "scan_unit_failure_category_counts" in html
    assert "File failures" in html
    assert "Shard failures" in html
    assert "Scan-unit failures" in html
