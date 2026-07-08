ALTER TABLE manifests ADD COLUMN IF NOT EXISTS worker_integrity_status VARCHAR(32);
ALTER TABLE manifests ADD COLUMN IF NOT EXISTS worker_integrity_requested_at TIMESTAMPTZ;
ALTER TABLE manifests ADD COLUMN IF NOT EXISTS worker_integrity_started_at TIMESTAMPTZ;
ALTER TABLE manifests ADD COLUMN IF NOT EXISTS worker_integrity_finished_at TIMESTAMPTZ;
ALTER TABLE manifests ADD COLUMN IF NOT EXISTS worker_integrity_server_id VARCHAR(128);
ALTER TABLE manifests ADD COLUMN IF NOT EXISTS worker_integrity_report_json TEXT NOT NULL DEFAULT '{}';
