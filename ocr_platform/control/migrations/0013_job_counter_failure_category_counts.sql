ALTER TABLE job_counters
ADD COLUMN IF NOT EXISTS failure_category_counts_json TEXT NOT NULL DEFAULT '{}';

INSERT INTO schema_migrations (version)
VALUES ('0013_job_counter_failure_category_counts')
ON CONFLICT (version) DO NOTHING;
