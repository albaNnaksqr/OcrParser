# v0.3.3 Lease Attempt Cap Maintenance Release

English | [中文](release-v0.3.3.zh-CN.md)

v0.3.3 is a focused Control scheduling and recovery maintenance release. It
prevents lease-expiry recovery from exceeding a Job's configured shard-attempt
limit and strengthens concurrent terminal-state handling.

It does not change the CLI, HTTP/OpenAPI contracts, database schema or
migration history, manifest and output formats, status vocabulary, or Parser
algorithms.

## Recovery Changes

- When a running shard lease expires at `max_shard_attempts`, the shard and its
  current attempt now become terminal `failed` with the bounded
  `lease_expired` category. The claim path does not create an `N+1` attempt.
- A requested Job stop takes precedence over lease-exhaustion failure, so
  affected shard, attempt, and Job records converge to the existing stopped
  outcome.
- Terminal shard updates remain monotonic and idempotent. Late replay cannot
  regress an exhausted or stopped shard, while mismatched server or attempt
  updates remain fenced.
- Job terminal attribution is serialized with shard terminal changes so
  concurrent success, failure, stop, reclaim, and replay paths converge
  deterministically.

The release adds PostgreSQL concurrency coverage for simultaneous claims,
lease exhaustion, stop, worker re-registration, heartbeat, and competing
terminal updates. It does not change claim ordering, lease durations, status
values, or the public API.

## Upgrade and Rollback

Use the normal Control maintenance procedure to deploy the exact v0.3.3 wheel.
No new migration is included. A deployment already current at migration
`0020_model_profile_certification` remains current:

```bash
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

Existing Agent configuration and command lines do not change. After upgrade,
monitor stale and failed shards to confirm exhausted leases settle with
`lease_expired` rather than receiving an additional attempt.

Because v0.3.3 does not change the schema, a Control rollback to v0.3.2 does
not require a database downgrade. The v0.3.2 rollback floor established by
migration 0020 still applies.

## Release Gate

Before tagging, verify the Python 3.10-3.12 test matrix, targeted SQLite and
PostgreSQL recovery tests, installation profiles, documentation links,
migration checksums, and package data. Build the release wheel from the final
clean commit and confirm that its version and source revision match the tag,
`dirty=false`, and `/source.json` reports `release_build=true`.

Publishing this maintenance release does not require starting an OCR or GPU
model service.
