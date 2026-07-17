# v0.3 Release Checklist

[中文](release-v0.3.zh-CN.md)

1. Confirm a clean public `main` and green Python 3.10-3.12 CI.
2. Build base/platform/s3/layout/full wheels in empty environments.
3. Verify PostgreSQL migrations/checksums, concurrent claim/lease/recovery,
   mock Control → Agent → Artifact, output audit, and the performance guard.
4. Confirm every UI module and migration SQL file is wheel package data.
5. Verify `_build_info.json`, `/source.json`, AGPL license/source routes, and the
   matching tag commit with `tools/verify_release_build.py`.
6. Publish `v0.3.0rc1`; real-model certification uses that immutable commit and
   does not run inside GitHub release jobs.
7. After candidate acceptance, update release notes and publish `v0.3.0` from a
   clean commit with the same checks.
