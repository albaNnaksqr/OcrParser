CREATE UNIQUE INDEX IF NOT EXISTS ux_shard_attempts_shard_attempt
ON shard_attempts (shard_id, attempt_number);

INSERT INTO schema_migrations (version)
VALUES ('0004_unique_shard_attempt_number')
ON CONFLICT (version) DO NOTHING;
