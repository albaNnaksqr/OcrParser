ALTER TABLE jobs
ALTER COLUMN input_mode TYPE VARCHAR(64);

ALTER TABLE manifests
ALTER COLUMN input_mode TYPE VARCHAR(64);

INSERT INTO schema_migrations (version)
VALUES ('0018_widen_input_mode_columns')
ON CONFLICT (version) DO NOTHING;
