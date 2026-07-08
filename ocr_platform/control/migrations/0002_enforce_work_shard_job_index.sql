-- Enforce job-global work shard indexes.
--
-- Distributed scan units can write separate manifest fragments, but shard_index
-- is a job-global execution identifier. This unique index prevents duplicate
-- global shard numbers across fragments.

BEGIN;

CREATE UNIQUE INDEX IF NOT EXISTS ux_work_shards_job_index
ON work_shards (job_id, shard_index);

INSERT INTO schema_migrations (version)
VALUES ('0002_enforce_work_shard_job_index')
ON CONFLICT (version) DO NOTHING;

COMMIT;
