CREATE INDEX IF NOT EXISTS ix_jobs_archived_created
ON jobs (archived_at, created_at);

INSERT INTO schema_migrations (version)
VALUES ('0012_jobs_default_list_index')
ON CONFLICT (version) DO NOTHING;
