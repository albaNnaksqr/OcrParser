-- OCR Platform control-plane schema baseline for PostgreSQL.
--
-- This file is intended for production database provisioning and audit. The
-- Python startup path still performs compatibility checks for local/dev
-- convenience, but production PostgreSQL deployments should apply SQL
-- migrations explicitly before starting the control service.

BEGIN;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version VARCHAR(128) PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS servers (
    id VARCHAR(128) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    host VARCHAR(255) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'offline',
    capacity_slots INTEGER NOT NULL DEFAULT 1,
    capabilities_json TEXT NOT NULL DEFAULT '{}',
    last_heartbeat_at TIMESTAMPTZ,
    archived_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS model_profiles (
    id VARCHAR(128) PRIMARY KEY,
    label VARCHAR(255) NOT NULL,
    engine VARCHAR(64) NOT NULL,
    ip VARCHAR(255),
    port INTEGER,
    model_name VARCHAR(255),
    page_concurrency INTEGER,
    extra_args_json TEXT NOT NULL DEFAULT '{}',
    api_key TEXT,
    api_key_env_var VARCHAR(255),
    requires_api_key BOOLEAN NOT NULL DEFAULT FALSE,
    is_default BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS jobs (
    id VARCHAR(36) PRIMARY KEY,
    input_dir TEXT NOT NULL,
    output_dir TEXT NOT NULL,
    engine VARCHAR(64) NOT NULL,
    input_mode VARCHAR(64) NOT NULL DEFAULT 'directory',
    model_profile_id VARCHAR(128),
    manifest_root TEXT,
    target_files_per_shard INTEGER NOT NULL DEFAULT 1000,
    max_shard_attempts INTEGER NOT NULL DEFAULT 3,
    engine_config TEXT,
    assigned_server_id VARCHAR(128) NOT NULL REFERENCES servers(id),
    allowed_server_ids_json TEXT NOT NULL DEFAULT '[]',
    status VARCHAR(32) NOT NULL DEFAULT 'queued',
    failure_category VARCHAR(64),
    error_message TEXT,
    ip VARCHAR(255),
    port INTEGER,
    model_name VARCHAR(255),
    page_concurrency INTEGER,
    extra_args_json TEXT NOT NULL DEFAULT '{}',
    command_json TEXT NOT NULL DEFAULT '[]',
    force_reprocess BOOLEAN NOT NULL DEFAULT FALSE,
    stop_requested BOOLEAN NOT NULL DEFAULT FALSE,
    archived_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS manifests (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    input_mode VARCHAR(64) NOT NULL,
    input_root TEXT,
    manifest_path TEXT NOT NULL,
    meta_path TEXT,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes BIGINT NOT NULL DEFAULT 0,
    next_shard_index INTEGER NOT NULL DEFAULT 1,
    scanner_version VARCHAR(32) NOT NULL DEFAULT '1',
    status VARCHAR(32) NOT NULL DEFAULT 'ready',
    frozen_at TIMESTAMPTZ,
    freeze_report_json TEXT NOT NULL DEFAULT '{}',
    worker_integrity_status VARCHAR(32),
    worker_integrity_requested_at TIMESTAMPTZ,
    worker_integrity_started_at TIMESTAMPTZ,
    worker_integrity_finished_at TIMESTAMPTZ,
    worker_integrity_server_id VARCHAR(128),
    worker_integrity_report_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS work_shards (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    manifest_id BIGINT NOT NULL REFERENCES manifests(id) ON DELETE CASCADE,
    shard_index INTEGER NOT NULL,
    shard_path TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    assigned_server_id VARCHAR(128),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    file_count INTEGER NOT NULL DEFAULT 0,
    processed_files INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    skipped_files INTEGER NOT NULL DEFAULT 0,
    completed_pages INTEGER NOT NULL DEFAULT 0,
    api_inflight INTEGER NOT NULL DEFAULT 0,
    api_inflight_peak INTEGER NOT NULL DEFAULT 0,
    api_waiting INTEGER NOT NULL DEFAULT 0,
    oldest_api_inflight FLOAT NOT NULL DEFAULT 0,
    execution_paused BOOLEAN NOT NULL DEFAULT FALSE,
    api_concurrency_limit INTEGER,
    execution_control_reason TEXT,
    failure_category VARCHAR(64),
    error_message TEXT,
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS shard_attempts (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    shard_id BIGINT NOT NULL REFERENCES work_shards(id) ON DELETE CASCADE,
    attempt_number INTEGER NOT NULL,
    server_id VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    processed_files INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    skipped_files INTEGER NOT NULL DEFAULT 0,
    completed_pages INTEGER NOT NULL DEFAULT 0,
    execution_paused BOOLEAN NOT NULL DEFAULT FALSE,
    api_concurrency_limit INTEGER,
    execution_control_reason TEXT,
    failure_category VARCHAR(64),
    error_message TEXT,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

CREATE TABLE IF NOT EXISTS scan_units (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    assigned_server_id VARCHAR(128),
    attempt_count INTEGER NOT NULL DEFAULT 0,
    manifest_path TEXT,
    meta_path TEXT,
    file_count INTEGER NOT NULL DEFAULT 0,
    total_bytes BIGINT NOT NULL DEFAULT 0,
    failure_category VARCHAR(64),
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    finished_at TIMESTAMPTZ,
    lease_expires_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS job_files (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    file_path TEXT NOT NULL,
    filename VARCHAR(512) NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    total_pages INTEGER,
    done_pages INTEGER NOT NULL DEFAULT 0,
    output_path TEXT,
    error TEXT,
    failure_category VARCHAR(64),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS job_events (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    event_type VARCHAR(64) NOT NULL,
    file_path TEXT,
    page_no INTEGER,
    status VARCHAR(64),
    failure_category VARCHAR(64),
    payload_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS job_counters (
    job_id VARCHAR(36) PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
    started_files INTEGER NOT NULL DEFAULT 0,
    completed_files INTEGER NOT NULL DEFAULT 0,
    failed_files INTEGER NOT NULL DEFAULT 0,
    skipped_files INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER NOT NULL DEFAULT 0,
    completed_pages INTEGER NOT NULL DEFAULT 0,
    degraded_pages INTEGER NOT NULL DEFAULT 0,
    recent_failed_files_json TEXT NOT NULL DEFAULT '[]',
    recent_errors_json TEXT NOT NULL DEFAULT '[]',
    failure_category_counts_json TEXT NOT NULL DEFAULT '{}',
    first_event_at TIMESTAMPTZ,
    last_event_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS job_logs (
    id BIGSERIAL PRIMARY KEY,
    job_id VARCHAR(36) NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    server_id VARCHAR(128) NOT NULL,
    stream VARCHAR(16) NOT NULL,
    line TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ix_jobs_status_created ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_archived_created ON jobs (archived_at, created_at);
CREATE INDEX IF NOT EXISTS ix_jobs_archived_status_created ON jobs (archived_at, status, created_at);
CREATE INDEX IF NOT EXISTS ix_work_shards_job_status_index ON work_shards (job_id, status, shard_index);
CREATE INDEX IF NOT EXISTS ix_work_shards_job_server_status ON work_shards (job_id, assigned_server_id, status);
CREATE INDEX IF NOT EXISTS ix_work_shards_job_failure_status ON work_shards (job_id, failure_category, status, shard_index);
CREATE INDEX IF NOT EXISTS ix_work_shards_job_status_started ON work_shards (job_id, status, started_at, shard_index);
CREATE UNIQUE INDEX IF NOT EXISTS ux_work_shards_job_index ON work_shards (job_id, shard_index);
CREATE UNIQUE INDEX IF NOT EXISTS ux_work_shards_manifest_index ON work_shards (manifest_id, shard_index);
CREATE INDEX IF NOT EXISTS ix_scan_units_job_status ON scan_units (job_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS ux_scan_units_job_path ON scan_units (job_id, path);
CREATE INDEX IF NOT EXISTS ix_job_events_job_created ON job_events (job_id, created_at);
CREATE INDEX IF NOT EXISTS ix_job_events_job_created_id ON job_events (job_id, created_at, id);
CREATE INDEX IF NOT EXISTS ix_job_events_job_failure_created ON job_events (job_id, failure_category, created_at, id);
CREATE INDEX IF NOT EXISTS ix_job_files_job_status ON job_files (job_id, status);
CREATE INDEX IF NOT EXISTS ix_job_files_job_path ON job_files (job_id, file_path);
CREATE INDEX IF NOT EXISTS ix_job_files_job_updated_id ON job_files (job_id, updated_at, id);
CREATE INDEX IF NOT EXISTS ix_job_logs_job_created ON job_logs (job_id, created_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS ux_shard_attempts_shard_attempt ON shard_attempts (shard_id, attempt_number);
CREATE INDEX IF NOT EXISTS ix_shard_attempts_job_status ON shard_attempts (job_id, status);

INSERT INTO schema_migrations (version)
VALUES ('0001_control_schema')
ON CONFLICT (version) DO NOTHING;

COMMIT;
