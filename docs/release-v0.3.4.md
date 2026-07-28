# v0.3.4 PostgreSQL Claim Stability Maintenance Release

English | [中文](release-v0.3.4.zh-CN.md)

v0.3.4 is a focused Control scheduling maintenance release. It removes a
PostgreSQL lock-order inversion between concurrent shard claims and terminal
shard updates.

It does not change the CLI, HTTP/OpenAPI contracts, database schema or
migration history, manifest and output formats, status vocabulary, claim
ordering, or Parser algorithms.

## Stability Fix

- A shard claim now acquires a shared lock on its parent Job before selecting
  and locking a WorkShard.
- Concurrent claimers remain compatible with one another, and WorkShard
  selection continues to use the existing `FOR UPDATE SKIP LOCKED` behavior.
- Terminal updates keep their existing Job-to-WorkShard lock order, removing
  the inverse lock dependency that could deadlock under PostgreSQL concurrency.
- Existing eligibility checks, attempt fencing, lease recovery, terminal-state
  monotonicity, and replay behavior are unchanged.

The maintenance candidate was checked with deterministic collision coverage,
multi-worker PostgreSQL claim stress, the normal test matrix, and package
validation. These gates exercise Control stability only; they do not require
starting an OCR or GPU model service.

## Upgrade and Rollback

Deploy the exact v0.3.4 wheel through the normal Control maintenance procedure.
No new migration is included. A deployment already current at migration
`0020_model_profile_certification` remains current:

```bash
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

Existing Agent configuration and command lines do not change. Because v0.3.4
does not change the schema, rollback to v0.3.3 does not require a database
downgrade. The v0.3.2 rollback floor established by migration 0020 still
applies.

## Release Integrity

The release wheel must be built from the final clean `v0.3.4` commit. Its
embedded source revision must match the tag, `dirty=false`, and `/source.json`
must report `release_build=true` with the corresponding public source URL.
