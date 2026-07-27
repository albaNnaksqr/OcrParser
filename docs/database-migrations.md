# Database migrations

OcrParser keeps the ordered PostgreSQL SQL history in
`ocr_platform/control/migrations/`. A single `MigrationRunner` is used by
Control startup checks, Deployment Doctor, CI, and the migration CLI. It does
not use Alembic and does not rewrite historical migrations.

Install the platform extra and set the production database URL:

```bash
export OCR_PLATFORM_DATABASE_URL='postgresql+psycopg://user:password@db/ocr_platform'
export OCR_PLATFORM_AUTO_MIGRATE=0
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

PostgreSQL startup migration is disabled by default. Run `plan`, `apply`, and
`verify` before restarting Control so failures happen before the new process
serves traffic. Set `OCR_PLATFORM_AUTO_MIGRATE=1` only when startup-owned
migration is an explicit deployment decision. Set
`OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS=1` when schema drift must fail startup;
otherwise the process stays alive, `/readyz` and business APIs report `503`, and
the diagnostics endpoints remain available.

Applying migrations while Control is already running makes readiness recover,
but intentionally does not create default model profiles from a read request.
Restart Control after `verify`, or explicitly run:

```bash
python -m ocr_platform.control.bootstrap
```

Keep database backups and test migrations on a staging copy before production
rollout. Systemd units do not run migrations implicitly.
