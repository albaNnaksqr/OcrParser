ALTER TABLE schema_migrations
    ADD COLUMN IF NOT EXISTS checksum VARCHAR(64);

INSERT INTO schema_migrations (version)
VALUES ('0019_schema_migration_checksums')
ON CONFLICT (version) DO NOTHING;
