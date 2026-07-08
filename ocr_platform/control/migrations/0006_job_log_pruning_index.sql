CREATE INDEX IF NOT EXISTS ix_job_logs_job_created
ON job_logs (job_id, created_at, id);

INSERT INTO schema_migrations (version)
VALUES ('0006_job_log_pruning_index')
ON CONFLICT (version) DO NOTHING;
