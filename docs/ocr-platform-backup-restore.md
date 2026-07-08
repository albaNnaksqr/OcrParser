# OCR Platform Production Backup and Restore Runbook

This runbook is for production-scale OCR jobs with millions of PDFs. Platform
state has three parts:

- PostgreSQL control-plane state: jobs, workers, manifest metadata, shard
  metadata, leases, attempts, aggregate counters, and model profiles.
- Shared-disk job snapshots: JSONL manifest files, scan-unit manifests, shard
  JSONL files, metadata files, and freeze report data under `manifest_root`.
- OCR output directories: Markdown, JSON, PDF, and other artifacts for each
  source PDF, plus the `.ocr_status.json` sidecar.

Production backup must cover both PostgreSQL and the shared-disk manifest/shard
files. Whether OCR outputs are part of the same backup policy depends on
whether the business accepts rerunning OCR after restore.

## Backup Principles

PostgreSQL is the source of truth for scheduling state, but it does not store
the full PDF list for very large jobs. The full input snapshot lives in JSONL
manifest and shard files under `manifest_root`. Backing up only PostgreSQL is
not enough. Backing up only shared-disk files is not enough either.

Minimum recommended policy:

- Run a daily full `pg_dump` and keep WAL/PITR capability.
- After every large job is created, back up that job's `manifest_root/<job_id>/`
  directory.
- After gray or production batches finish, back up the freeze report,
  manifest/shard JSONL files, and OCR output when output reuse matters.
- Back up `/etc/ocr-platform/control.env` only through an encrypted secret
  backup or a secret manager. Do not put plaintext API tokens or model profile
  API keys in a general backup package.

If production uses `OCR_JOB_FILE_DETAIL_LIMIT=0` or
`OCR_JOB_EVENT_DETAIL_LIMIT=0`, `job_files` and `job_events` intentionally do
not preserve full per-PDF detail. Restore and audit should rely on PostgreSQL
aggregate state, shard state, sidecars, and manifest integrity checks.

## PostgreSQL Backup

Record the schema migration state before backup:

```bash
psql "$OCR_PLATFORM_DATABASE_URL" \
  -c "select version, applied_at from schema_migrations order by version;"
```

Logical backup example:

```bash
mkdir -p /backup/ocr-platform/postgres
pg_dump "$OCR_PLATFORM_DATABASE_URL" \
  --format=custom \
  --file "/backup/ocr-platform/postgres/ocr_platform-$(date +%Y%m%d-%H%M%S).dump"
```

Also ask the DBA or cloud database owner to configure WAL archiving or PITR.
`pg_dump` is useful for drills and cross-environment restore; PITR is the safer
tool for point-in-time incident recovery.

## Manifest And Shard Backup

`manifest_root` is usually on shared storage, for example:

```text
/shared/.ocr_platform/manifests/<job_id>/
```

The directory usually contains:

- Root JSONL manifest or a distributed-scan aggregate path.
- Scan-unit manifests, one JSONL fragment for each completed scan unit.
- Shard JSONL files that workers actually claim and execute.
- Metadata files with file counts, byte totals, scan parameters, and samples.

Backup example:

```bash
JOB_ID=<job-id>
MANIFEST_ROOT=/shared/.ocr_platform/manifests
mkdir -p /backup/ocr-platform/manifests
rsync -a --delete \
  "$MANIFEST_ROOT/$JOB_ID/" \
  "/backup/ocr-platform/manifests/$JOB_ID/"
```

If shared storage already has snapshots, use them, but keep an explicit visible
backup of important `manifest_root/<job_id>/` directories so restore checks can
compare the exact manifest and shard files.

## OCR Output And Sidecars

OCR output paths are determined by `output_dir + relative_path`. Each PDF should
have a `.ocr_status.json` sidecar recording status, failure category, page
counts, duration, model configuration summary, and error type.

If the business requires restore without repeating successfully completed PDFs,
back up output directories and sidecars together:

```bash
JOB_ID=<job-id>
rsync -a --delete \
  /shared/ocr-output/$JOB_ID/ \
  /backup/ocr-platform/output/$JOB_ID/
```

If rerunning OCR is acceptable, output directories may be treated as
rebuildable, but after restore confirm `force_reprocess=false` so the parser can
skip completed PDFs only when sidecar freshness and artifact completeness prove
the old output is reusable.

After restore or gray validation, audit output directly from the manifest/shard
JSONL:

```bash
cd /opt/ocr-platform/ocrparser
python3 tools/audit_manifest_outputs.py \
  --manifest /shared/.ocr_platform/manifests/<job-id>/shards/shard-000001.jsonl \
  --output-dir /shared/ocr-output/<job-id> \
  --check-input
```

The report groups `sidecar_missing`, `artifact_missing`, `artifact_invalid`,
`input_missing`, `input_changed`, and similar categories in
`issues_by_category`, while keeping bounded `issue_samples` so audit does not
recreate a full per-PDF detail table in PostgreSQL.

## Restore Flow

1. Stop the control service and all agents so no worker can claim shards during
   restore.
2. Restore PostgreSQL:

```bash
createdb ocr_platform_restore
pg_restore \
  --dbname "postgresql+psycopg://<user>:<password>@<host>:5432/ocr_platform_restore" \
  /backup/ocr-platform/postgres/<dump-file>.dump
```

If your DBA uses a native `psql` connection string instead of a SQLAlchemy DSN,
adapt the command to the local database standard.

3. Confirm `schema_migrations`:

```bash
psql "$OCR_PLATFORM_DATABASE_URL" \
  -c "select version, applied_at from schema_migrations order by version;"
```

4. Restore manifest/shard files:

```bash
JOB_ID=<job-id>
MANIFEST_ROOT=/shared/.ocr_platform/manifests
rsync -a --delete \
  "/backup/ocr-platform/manifests/$JOB_ID/" \
  "$MANIFEST_ROOT/$JOB_ID/"
```

5. Restore the OCR output directory if this batch must avoid repeated OCR.
6. Start the control service, but keep agents stopped.
7. Validate critical jobs:

```bash
curl -H "Authorization: Bearer $OCR_PLATFORM_API_TOKEN" \
  "http://<control-host>:8080/api/jobs/{job_id}/manifest/integrity"

curl -H "Authorization: Bearer $OCR_PLATFORM_API_TOKEN" \
  "http://<control-host>:8080/api/jobs/{job_id}/manifest/freeze-report"
```

`/api/jobs/{job_id}/manifest/integrity` should return `ok=true`. If manifest
files exist but shard file counts differ from the database, do not start
agents; restore the correct manifest/shard files or rebuild the job snapshot.

`/api/jobs/{job_id}/manifest/freeze-report` confirms whether scanning was
frozen. If the restored freeze report says scanning was still in progress,
inspect pending/running scan units before deciding whether to continue scanning
or abandon and recreate the job.

8. Start a small number of agents. Watch heartbeats, shard claims, and progress.
   Restore all workers only after claim and progress behavior is clean.

## Rebuildable And Not Rebuildable

Rebuildable:

- A folder-snapshot job that has not started execution can be scanned again.
- If the original PDF input directory is unchanged, a manifest can be generated
  again, although shard numbering and job history may not match the original
  job exactly.
- OCR outputs are rebuildable only when the business accepts repeated OCR.

Not safely rebuildable:

- PostgreSQL scheduling state for a job that has entered production execution.
- Frozen manifest and shard files, especially shards already claimed by workers.
- `.ocr_status.json` sidecars and output artifacts when the business relies on
  skipping PDFs that already succeeded.

For production incident recovery, prefer restoring the original job from a
PostgreSQL dump plus `manifest_root` backup. Rescan and create a new job only
when the old job had not entered execution or duplicate OCR work is acceptable.

## Post-Restore Acceptance Checklist

- `schema_migrations` contains every migration required by the current release.
- The control `/ui/` is reachable, and Workers does not show a mixed-version
  warning.
- `/api/jobs/{job_id}/manifest/integrity` returns `ok=true` for critical jobs.
- `/api/jobs/{job_id}/manifest/freeze-report` matches the expected scan state.
- JSONL manifest and shard files exist under `manifest_root/<job_id>/`.
- Output directories contain `.ocr_status.json`, and artifact completeness
  checks do not treat partial output as success.
- After a small agent restart, shard claims have no duplicates and stale /
  attempt-aware protections still behave correctly.

## Regular Drill

Run a restore drill at least monthly:

1. Restore the latest `pg_dump` into a temporary PostgreSQL database.
2. Restore one completed job's `manifest_root/<job_id>/` with `rsync`.
3. Start a temporary control service and run manifest integrity and freeze
   report checks.
4. Run a PostgreSQL claim stress check against the temporary database:

```bash
python tools/pg_claim_stress.py \
  --database-url "$OCR_PLATFORM_DATABASE_URL" \
  --shards 1000 \
  --scan-units 1000 \
  --scan-unit-shards 2 \
  --workers 64 \
  --json
```

Acceptance criteria: `ok=true`, `duplicate_claims={}`, `missing_claims=0`,
`attempt_conflict_rejected=true`, `scan_unit_claims.ok=true`, and
`scan_unit_completion_shards.ok=true`.
