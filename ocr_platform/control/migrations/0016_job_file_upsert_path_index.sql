CREATE INDEX IF NOT EXISTS ix_job_files_job_path
ON job_files (job_id, file_path);

INSERT INTO schema_migrations (version)
VALUES ('0016_job_file_upsert_path_index')
ON CONFLICT (version) DO NOTHING;
