# v0.4.0rc1 Release Candidate

English | [中文](release-v0.4.0rc1.zh-CN.md)

`0.4.0rc1` is the release candidate for the v0.4 Control operational-maturity
line. It keeps Control as a modular monolith and completes the planned
operations, ownership, and compatibility work without changing Parser
algorithms or the scheduling algorithm.

## Highlights

- PostgreSQL startup migration is opt-in; health and readiness are separate,
  and schema drift blocks business APIs with actionable diagnostics.
- Model Profiles can carry certification and immutable provenance. Enforcement
  remains opt-in, and existing profiles remain non-blocking.
- Prometheus metrics use bounded labels. Diagnostics expose advisory capacity,
  audit, and alert information without automatic scaling.
- Application commands own transactions; queries remain read-only; scheduling
  owns claim, lease, attempt, fencing, replay, and recovery transitions.
- Jobs, manifests, and workers have explicit command/query/policy ownership.
- The legacy `ocr_platform.control.service` façade is removed after all
  repository consumers were migrated.

Removing that façade is the only intentional Python import breaking change in
v0.4. CLI, HTTP/OpenAPI, database schema and migrations, status values,
manifest/output formats, and Parser compatibility imports remain unchanged.

## Candidate Upgrade

Use the [v0.4 upgrade guide](migration-v0.4.md) and the
[façade symbol migration table](control-facade-migration.md). PostgreSQL
deployments should keep startup auto-migration disabled and use
`ocr-platform-migrate plan|apply|verify`.

The schema ceiling remains migration `0020`; the candidate adds no `0021`.
Rollback prefers the latest validated v0.3 maintenance wheel, with v0.3.2 as
the oldest compatible floor after `0020`.

## Operations

Review the current [Control module map](control-module-map.md) and
[alert examples](control-alerts.md). Metrics and diagnostics must remain
protected by the existing API token. Never use job IDs, file paths, arbitrary
error text, credentials, endpoints, or document content as metric labels or
published evidence.

## RC Integrity

The candidate wheel must be built from the clean commit used for validation.
Its embedded `source_revision` must be the commit SHA referenced by the
`v0.4.0rc1` tag, not the literal tag name. The installed distribution version
must be `0.4.0rc1`; both wheel provenance and `/source.json` must report that
commit with `dirty=false` and `release_build=true`.

Any wheel built before the release commit and tag exist is a packaging
preflight artifact only. After the release commit is created and
`v0.4.0rc1` points to it, rebuild the wheel from that exact clean commit and
repeat the provenance and installation checks before publishing it.

Automated package, migration, recovery, security, contract, and installation
gates must pass before the RC is tagged. Isolated recovery and engine
integration checks follow as release gates; they assess the exact candidate
and do not compare model quality or throughput with historical deployments.
No GPU model service is required to build or publish the candidate.

This document describes a candidate, not the final `v0.4.0` release. Final
promotion requires acceptance of the exact RC with no tracked runtime changes.
