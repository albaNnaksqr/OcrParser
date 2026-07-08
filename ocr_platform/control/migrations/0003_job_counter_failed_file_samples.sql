ALTER TABLE job_counters
ADD COLUMN IF NOT EXISTS recent_failed_files_json TEXT NOT NULL DEFAULT '[]';

INSERT INTO schema_migrations (version)
VALUES ('0003_job_counter_failed_file_samples')
ON CONFLICT (version) DO NOTHING;
