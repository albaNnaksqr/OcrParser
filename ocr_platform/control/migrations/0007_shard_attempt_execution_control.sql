ALTER TABLE shard_attempts
ADD COLUMN IF NOT EXISTS execution_paused BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE shard_attempts
ADD COLUMN IF NOT EXISTS api_concurrency_limit INTEGER;

ALTER TABLE shard_attempts
ADD COLUMN IF NOT EXISTS execution_control_reason TEXT;

INSERT INTO schema_migrations (version)
VALUES ('0007_shard_attempt_execution_control')
ON CONFLICT (version) DO NOTHING;
