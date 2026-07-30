# Upgrading to v0.4

English | [中文](migration-v0.4.zh-CN.md)

This guide covers the `0.4.0rc2` candidate. Validate the exact candidate wheel
in an isolated environment before production use. The public repository remains
the only source-code mainline.

## Compatibility

v0.4 preserves:

- the existing console scripts, CLI flags, and exit codes;
- HTTP paths, existing fields, status codes, and OpenAPI behavior;
- database schema and migration history through
  `0020_model_profile_certification`;
- Job, Shard, Attempt, manifest, output, and fallback wire formats; and
- Parser algorithms and top-level Parser compatibility imports.

Removing `ocr_platform.control.service` is the only intentional Python import
breaking change in v0.4. Follow the
[symbol migration table](control-facade-migration.md) before upgrading custom
Python integrations.

## Before Upgrade

1. Back up PostgreSQL and record the deployed wheel, source revision, and
   current migration status.
2. Confirm the database has no checksum mismatch or unknown migration:

   ```bash
   ocr-platform-migrate status --database-url "$OCR_PLATFORM_DATABASE_URL"
   ocr-platform-migrate plan --database-url "$OCR_PLATFORM_DATABASE_URL"
   ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
   ```

3. Confirm Control and Agent configuration contains no saved credentials or
   private endpoints in source-controlled files.
4. Review Model Profile certification enforcement. Existing profiles without a
   certification record remain compatible and behave as
   `contract_only` with enforcement `off`.

## Deploy

Install the exact RC wheel with the required extras, then apply and verify
migrations explicitly:

```bash
python -m pip install 'ocrparser-platform[platform]==0.4.0rc2'
ocr-platform-migrate apply --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

For PostgreSQL, startup auto-migration is disabled unless
`OCR_PLATFORM_AUTO_MIGRATE=1` is explicitly set. Keep it disabled in production
and use the migration CLI. SQLite retains its local-development convenience
behavior.

Start Control and check both health surfaces:

```bash
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/readyz
```

`/healthz` reports process health. `/readyz` returns `503` while PostgreSQL
schema drift blocks business APIs and includes the migration commands required
to recover readiness.

Agents may provide a sanitized provenance file with
`--engine_provenance_file` or `OCR_AGENT_ENGINE_PROVENANCE_FILE`. Heartbeats
publish revisions and digests only; do not place endpoints, credentials, local
paths, or document content in that file.

## Operational Checks

- Query `/api/system/metrics` with the existing API token and load the
  [alert examples](control-alerts.md).
- Review diagnostics `capacity`, `audit`, and `alerts`. Capacity values are
  advisory and do not automatically scale workers or model services.
- Verify certification preflight before enabling `verified` or `certified`
  enforcement.
- Confirm `/source.json` reports version `0.4.0rc2`, the exact wheel revision,
  `build_dirty=false`, and `release_build=true`.

## Rollback

v0.4 adds no migration after `0020`. Prefer rollback to the latest validated
v0.3 maintenance wheel. The oldest supported rollback floor for a database
containing `0020` is v0.3.2, after migration, Worker, and certification
compatibility checks. Do not run v0.3.1 against that database. Rolling back
below v0.3.2 requires restoring a verified pre-0020 snapshot.

Stop new submissions before rollback, allow or stop active work according to
the recovery runbook, deploy the selected wheel, run migration `verify`, and
then restore Agents. A checksum mismatch, unknown migration, stuck replay, or
data-integrity failure blocks rollback completion.
