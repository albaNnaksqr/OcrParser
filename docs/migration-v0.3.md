# Migrating from v0.2 to v0.3

[中文](migration-v0.3.zh-CN.md)

v0.3 preserves CLI flags and exit codes, HTTP paths and schemas, the Job/Shard
state machine, migrations 0001-0018, manifest JSONL, and output formats. The
intentional installation change is that the default wheel now contains only the
Parser and remote-engine clients.

## Choose an installation profile

| Use case | Install command |
| --- | --- |
| Parser and remote OCR services | `pip install ocrparser-platform` |
| Control, Agent, PostgreSQL | `pip install 'ocrparser-platform[platform]'` |
| S3 helpers | `pip install 'ocrparser-platform[s3]'` |
| Local PP-DocLayout service | `pip install 'ocrparser-platform[layout]'` |
| v0.2-equivalent non-GPU runtime | `pip install 'ocrparser-platform[full]'` |
| Contributor environment | `pip install 'ocrparser-platform[dev]'` |

`full` does not install the hardware-specific layout GPU runtime. Existing
console-script names remain installed; a missing extra produces an actionable
install command instead of a raw import traceback.

## Upgrade the database

Migrations 0001-0018 are unchanged. Migration 0019 backfills checksums for the
historical records. Production deployments should run the shared runner before
restarting Control:

```bash
ocr-platform-migrate plan --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate apply --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

Startup auto-upgrade remains enabled in v0.3, but the explicit CLI is the
recommended operational path.

## Review compatibility and security

- `ocr_platform.control.service` remains a v0.3 compatibility façade; import
  new integrations from the owning Control domain.
- Legacy `success_fallback_text` and `success_fallback_image` remain valid;
  consumers should migrate to structured `stages` and `fallback` metadata.
- Control still defaults to loopback, Remote Admin remains opt-in, browser auth
  remains session-only, and database-saved model keys remain disabled by
  default.
- Verify `/source.json` reports the deployed wheel revision and
  `release_build=true` for release deployments.
