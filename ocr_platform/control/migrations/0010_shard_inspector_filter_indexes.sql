CREATE INDEX IF NOT EXISTS ix_work_shards_job_failure_status
ON work_shards (job_id, failure_category, status, shard_index);

CREATE INDEX IF NOT EXISTS ix_work_shards_job_status_started
ON work_shards (job_id, status, started_at, shard_index);

INSERT INTO schema_migrations (version)
VALUES ('0010_shard_inspector_filter_indexes')
ON CONFLICT (version) DO NOTHING;
