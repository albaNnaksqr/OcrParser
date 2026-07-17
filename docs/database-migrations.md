# Database migrations

OcrParser keeps the ordered PostgreSQL SQL history in
`ocr_platform/control/migrations/`. v0.3 adds a single `MigrationRunner` used by
Control startup, Deployment Doctor, CI, and the migration CLI. It does not use
Alembic and does not rewrite migrations `0001` through `0018`.

Install the platform extra and set the production database URL:

```bash
export OCR_PLATFORM_DATABASE_URL='postgresql+psycopg://user:password@db/ocr_platform'
ocr-platform-migrate status
ocr-platform-migrate plan
ocr-platform-migrate apply
ocr-platform-migrate verify
```

`apply` takes a PostgreSQL transaction advisory lock, applies pending SQL in
filename order, and records SHA-256 checksums. Migration `0019` adds the checksum
column and backfills the packaged checksums for historical records. `apply`
refuses to continue if an already-applied SQL file no longer matches its stored
checksum.

Control retains automatic startup migration in v0.3, but explicit `plan`,
`apply`, and `verify` are recommended during deployment so failures happen before
the service is restarted. Keep database backups and test migrations on a staging
copy before production rollout.
