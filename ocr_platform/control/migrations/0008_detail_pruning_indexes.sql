CREATE INDEX IF NOT EXISTS ix_job_events_job_created_id
ON job_events (job_id, created_at, id);

CREATE INDEX IF NOT EXISTS ix_job_files_job_updated_id
ON job_files (job_id, updated_at, id);

INSERT INTO schema_migrations (version)
VALUES ('0008_detail_pruning_indexes')
ON CONFLICT (version) DO NOTHING;
