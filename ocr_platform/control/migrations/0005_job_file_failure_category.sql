ALTER TABLE job_files
ADD COLUMN IF NOT EXISTS failure_category VARCHAR(64);

INSERT INTO schema_migrations (version)
VALUES ('0005_job_file_failure_category')
ON CONFLICT (version) DO NOTHING;
