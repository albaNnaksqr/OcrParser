ALTER TABLE job_counters
ADD COLUMN IF NOT EXISTS recent_errors_json TEXT NOT NULL DEFAULT '[]';

INSERT INTO schema_migrations (version)
VALUES ('0015_job_counter_recent_error_samples')
ON CONFLICT (version) DO NOTHING;
