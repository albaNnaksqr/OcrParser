ALTER TABLE job_events
ADD COLUMN IF NOT EXISTS failure_category VARCHAR(64);

CREATE INDEX IF NOT EXISTS ix_job_events_job_failure_created
ON job_events (job_id, failure_category, created_at, id);

INSERT INTO schema_migrations (version)
VALUES ('0014_job_event_failure_category')
ON CONFLICT (version) DO NOTHING;
