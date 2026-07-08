DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM scan_units
        GROUP BY job_id, path HAVING COUNT(*) > 1
    ) THEN
        RAISE EXCEPTION 'duplicate scan_units rows exist for the same job_id/path; deduplicate scan_units before applying 0011_unique_scan_unit_path';
    END IF;
END
$$;

CREATE UNIQUE INDEX IF NOT EXISTS ux_scan_units_job_path
ON scan_units (job_id, path);

INSERT INTO schema_migrations (version)
VALUES ('0011_unique_scan_unit_path')
ON CONFLICT (version) DO NOTHING;
