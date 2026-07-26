# v0.3.2 Database Compatibility Bridge

English | [中文](release-v0.3.2.zh-CN.md)

v0.3.2 is a narrow database compatibility release that prepares the Control
plane for v0.4 certified engine profiles. It does not enable certification
policy and does not change the existing CLI, HTTP/OpenAPI, job preflight,
state, manifest, or output contracts.

## Changes

- Migration `0020_model_profile_certification` adds an optional one-to-one
  certification-provenance record for each model profile.
- Existing model profiles are not backfilled and continue to behave exactly as
  they did in v0.3.1.
- PostgreSQL now applies checksum-verified migrations before ORM schema
  creation, making the migration catalog the production schema authority.
- SQLite retains create-all behavior for direct local development.

The new table is dormant in v0.3.2. No request or response field exposes it,
and no job is accepted or rejected based on it. It is designed for immutable
revisions and digests plus a future auditable risk-acceptance record; it must
not store credentials, private endpoints, OCR content, or customer documents.

## Upgrade

Back up the database, deploy the exact v0.3.2 wheel, then use the shared
migration runner:

```bash
ocr-platform-migrate plan --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate apply --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

Production deployments should verify that migration
`0020_model_profile_certification` is current before accepting new work.

## Rollback

The rollback floor after applying migration 0020 is v0.3.2. A later v0.4
Control can return to v0.3.2 without dropping the additive table, provided no
migration after 0020 has been applied.

Do not run v0.3.1 directly against a database containing migration 0020. Its
older migration catalog correctly treats 0020 as unexpected and blocks
production job preflight. Returning to v0.3.1 requires restoring a verified
pre-0020 database snapshot.

## Release Gate

Before tagging, verify the Python and installation matrices, SQLite and
PostgreSQL migration tests, concurrent migration apply, package data, exact
wheel provenance, documentation links, and the AGPL source offer. This
schema-only bridge does not require starting a GPU or OCR model service.
