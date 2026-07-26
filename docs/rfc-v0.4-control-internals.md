# RFC: OcrParser v0.4 Control Internals

English | [中文](rfc-v0.4-control-internals.zh-CN.md)

Status: Approved for phased implementation. On 2026-07-27 the maintainer
explicitly waived the prior 2026-08-02 calendar gate. All P0/P1,
single-writer, compatibility, review, and exit gates remain in force.

This RFC is subordinate to the [v0.4 Operational Maturity RFC](rfc-v0.4.md).
Its implementation must not delay production auto-migration/readiness policy,
certified engine profiles, observability, capacity planning, or auditability.
The current structure and proposed ownership boundaries are recorded in the
[Control Module Map](control-module-map.md).

## Context

The v0.3 domain split made `app.py` a small composition root and gave jobs,
workers, manifests, model profiles, remote administration, and diagnostics
separate routers. The remaining complexity is inside the domain cores and their
implicit cross-domain calls:

- jobs, manifests, and workers share scheduling state and call one another
  through lazy wrappers;
- transaction ownership is distributed across leaf helpers, queries, and HTTP
  adapters;
- scan, claim, lease, attempt, retry, fencing, and recovery behavior has no
  single internal owner;
- the `ocr_platform.control.service` compatibility façade exposes a large,
  monkeypatch-sensitive runtime surface;
- the current OpenAPI golden locks path and operation counts, not the complete
  schema, status, and error contract.

The audit snapshot behind these observations is documented in the module map.
File size is evidence for review, not a refactoring target or acceptance metric.

## Evidence Summary

The current snapshot contains:

- a 138-line composition root;
- 1,678-line jobs, 2,100-line manifests, and 1,059-line workers cores;
- 37 lazy cross-domain wrappers;
- 50 explicit transaction points across the four mutable cores;
- a 286-name compatibility façade used by eight test/tool files and 21
  monkeypatch sites;
- 13 centralized ORM classes;
- an OpenAPI golden covering 47 paths and 49 operations without locking the
  complete schema.

These figures are a point-in-time audit. The goal is explicit ownership and
testable behavior, not fewer lines.

## Goals

- Make application use cases the only place that coordinates multiple domains.
- Give each business operation exactly one commit or rollback boundary.
- Centralize scheduling state transitions and recovery policy.
- Eliminate private core imports between domains and all dependency cycles.
- Preserve every public HTTP, database, migration, state-machine, and output
  contract.
- Remove the historical Control service façade through an explicit migration.
- Make concurrency, late replay, and attempt fencing behavior independently
  testable against PostgreSQL.

## Non-goals

- Rewriting OCR, layout, table, Markdown, manifest, or scheduling algorithms.
- Splitting Control into microservices.
- Distributing ORM models by domain.
- Introducing a generic Repository or Unit of Work framework.
- Adopting Alembic, async database access, or an alternate ORM.
- Changing the Control UI framework. The size of `ui/main.js` remains recorded
  debt, but UI restructuring is outside this RFC.
- Changing HTTP paths, state values, CLI behavior, manifest JSONL, or artifact
  formats. Structural PRs in this RFC do not change the database schema or
  migrations 0001-0019; a separately reviewed additive operational migration
  remains governed by the parent RFC.

## Decisions

### Application use cases own coordination

HTTP routers translate requests and responses. Application use cases execute
business operations. A use case may invoke multiple domain policies or query
ports, but domain-private cores may not import one another.

Cross-domain outcomes are returned as explicit values. They must not depend on
dynamic façade attributes or monkeypatch forwarding.

### One transaction per business operation

A command or application use case owns one database transaction:

- it commits once on success;
- it rolls back once on failure;
- leaf mutation functions may `flush()` when identifiers or constraints must be
  observed, but do not commit or roll back;
- queries are read-only by default and never commit or roll back;
- transaction policy is enforced by static checks and integration tests.

Query modules must perform no DML. HTTP `GET` and `HEAD` handlers must not hide
DML inside a query. During compatibility migration, the existing job-summary
route may explicitly invoke the named `refresh_job_summary` application use
case and then perform a read. That route is the only allowlisted `GET` mutation;
the application-layer call must remain visible and independently testable. If
the refresh is later removed from the route, the allowlist becomes empty.

The current model-profile `GET` list path may create default profiles. Default
creation moves to an explicit startup/bootstrap initialization step before the
list query is made purely read-only.

### Scheduling becomes a non-HTTP internal domain

Create an internal `scheduling` domain with no router. It owns:

- `ScanUnit`, `WorkShard`, and `ShardAttempt` transition policy;
- scan-unit and shard claim selection;
- lease creation, renewal, expiry, and reclaim;
- attempt numbering and active-attempt validation;
- retry eligibility and retry status;
- restart fencing and late replay rejection;
- stop/recovery coordination for owned scheduling entities.

The scheduling domain does not own Job lifecycle, manifest construction and
integrity, or Server identity and heartbeat. Application use cases coordinate
those owners with scheduling.

### Entity ownership

| Owner | Primary entities and policy |
| --- | --- |
| jobs | `Job`, `JobCounter`, `JobEvent`, `JobLog`, `JobFile`; lifecycle, stop/archive/delete, counters, event and log policy |
| manifests | `Manifest`; snapshot construction, freeze, integrity, path validation |
| workers | `Server`; registration, heartbeat, status, capability and path eligibility |
| model profiles | `ModelProfile`; profile validation, provenance, effective model configuration |
| scheduling | `ScanUnit`, `WorkShard`, `ShardAttempt`; claim, lease, attempt, retry, fence, recovery |
| diagnostics | read-only health, migration, source and deployment diagnostics |
| remote admin | explicitly enabled remote-worker operations |

ORM declarations remain centralized in `ocr_platform.control.models`. Ownership
means policy and mutation authority, not physical model placement.

### State transition policy is explicit

Each owned stateful entity has one transition policy module. Callers request a
transition; they do not assign status strings directly. The policy defines:

- allowed source and target states;
- idempotent terminal replay;
- attempt and server fencing;
- timestamps, lease clearing, and terminal side effects;
- bounded failure and retry categories.

Static checks reject status mutation outside the owning policy, with narrowly
documented bootstrap or migration exceptions.

### Compatibility façade is removed last

`ocr_platform.control.service` remains until internal imports, tests, tools, and
documented integrations have migrated. Its removal is a separate PR after all
other Control changes are stable.

| Old import family | Replacement owner |
| --- | --- |
| job creation, lifecycle, events, logs, counters | `ocr_platform.control.domains.jobs` application/command/query surface |
| manifest creation, freeze, integrity | `ocr_platform.control.domains.manifests` surface |
| server registration, heartbeat, eligibility | `ocr_platform.control.domains.workers` surface |
| scan/shard claim, lease, attempts, recovery | explicit Control application/command surface; the internal scheduling domain is not a compatibility API |
| model profile configuration | `ocr_platform.control.domains.model_profiles` surface |
| deployment and source checks | `ocr_platform.control.domains.diagnostics` surface |
| remote worker operations | `ocr_platform.control.domains.remote_admin` surface |

Exact symbol-level mappings will be generated and checked before façade
removal. v0.4 release notes will identify this Python import compatibility
change.

## Dependency Rules

The intended dependency direction is:

1. centralized ORM and neutral Control contracts;
2. domain policy and read-only queries;
3. application use cases;
4. HTTP routers, CLI adapters, diagnostics adapters, and the composition root.

Domain-private `core` modules may depend on models and their own contracts, but
not on another domain's private core. Application use cases are the only
cross-domain coordinator. The scheduling domain remains internal and has no
HTTP dependency.

## Compatibility Contract

The refactor preserves:

- canonical OpenAPI paths, operations, request/response schemas, statuses, and
  error bodies;
- migrations 0001-0019 and their checksums, plus all existing database tables,
  columns, constraints, and state values. A new additive operational migration
  is independently reviewed and never mixed with a structural PR;
- Job/Shard/attempt behavior, including terminal monotonicity and replay
  idempotence;
- CLI flags and exit codes;
- manifest JSONL and output formats;
- authentication, remote-admin defaults, AGPL source offer, and package data.

The only intentional public Python change is removal of
`ocr_platform.control.service` at the end of v0.4. Domain-private modules remain
non-public.

## Sequenced Pull Requests

### PR 0: Documentation

Land this RFC and module map. No runtime behavior changes.

### PR 1: Complete contract and static gates

- Record canonical OpenAPI schemas, statuses, and error bodies, not only counts.
- Lock migration 0001-0019, database metadata, and state values.
- Add private cross-domain import, cycle, transaction, DML-on-read, and state
  mutation boundary checks.
- Add a symbol inventory for the service façade and migration table.

### PR 2: Auto-migration and readiness

Implement the operational RFC's production migration default and actionable
readiness result before structural work.

### PR 3: Certified engine profiles

Bind certification provenance and auditable risk acceptance without mixing the
change with Control decomposition. If provenance requires additive migration
0020, it is reviewed as an operational database change with the rollback gate
defined below.

### PR 4: Observability, capacity, and audit

Deliver bounded-label alerts, read-only capacity output, and durable audit
evidence. This remains ahead of internal decomposition.

### PR 5: Settings, bootstrap, and low-risk transactions

- Make settings and database/session construction explicit dependencies.
- Move transaction ownership from low-risk leaves into application use cases.
- Preserve all API and SQL behavior through characterization tests.

### PR 6: Scheduling kernel

- Introduce the non-HTTP scheduling domain.
- Move scan/shard claim, lease, attempt, retry, fencing, and recovery policy.
- Verify PostgreSQL contention, recovery, late replay, and attempt fencing.

### PR 7a: Split the jobs core

Move job lifecycle, projections, events, logs, and counters behind the jobs
owner. This PR is independently reversible.

### PR 7b: Split the manifests core

Move manifest construction, paths, freeze, and integrity behind the manifests
owner. This PR is independently reversible.

### PR 7c: Split the workers core

Move worker identity, heartbeat, eligibility, and preflight behind the workers
owner. Eliminate the remaining private-core cross-domain imports. This PR is
independently reversible.

PRs 7a-7c centralize the remaining transition policy and do not mix migrations,
API changes, or algorithm changes.

### PR 8: Remove the façade

Migrate all repository tests and tools, verify the symbol migration table, then
remove `ocr_platform.control.service`. Add release-note guidance.

### PR 9: Release candidate and release

Run the complete compatibility, packaging, PostgreSQL, recovery, security, and
source-offer gates before the v0.4 release.

Each structural PR must be independently mergeable and must not combine a
database migration, HTTP contract change, or algorithm change.

## Entry Gates

Runtime implementation is authorized immediately by the 2026-07-27 maintainer
override; the former date-based observation delay is no longer an entry
condition. The following gates still apply:

- the v0.3.1 long-running, multi-cycle validation remains reviewed under the
  recorded risk acceptance;
- no unresolved data-loss, duplicate-claim/artifact, migration, replay,
  shutdown, authentication, source-offer, or resource-leak issue remains;
- the complete contract baseline and rollback procedure are approved.

Any new P0/P1 freezes v0.4 and returns the defect to a v0.3.x maintenance
release.

## Exit Gates

- Complete canonical OpenAPI, status, and error contracts are unchanged.
- Migrations 0001-0019, their checksums, and existing database state values are
  unchanged. Any new operational migration is additive, independently gated,
  and absent from structural PRs.
- Private-core cross-domain imports: zero.
- Dependency cycles: zero.
- Commit/rollback calls in leaf mutations and queries: zero.
- DML in query modules: zero.
- Hidden DML in HTTP `GET` and `HEAD` handlers: zero. During compatibility
  migration, the explicit-DML allowlist contains only the named job-summary
  `refresh_job_summary` application call; it becomes empty if that refresh is
  removed.
- Stateful entity transitions are centralized in their owning policies.
- `ocr_platform.control.service` references and monkeypatch sites: zero.
- Real PostgreSQL concurrency, recovery, late replay, lease, and attempt-fencing
  tests pass.
- Full CI, Python and installation matrices, wheel/package data, and AGPL
  source-offer checks pass.

Line count is not an exit gate.

## Rollback

The structural PRs in this RFC contain no migration and retain a commit-level
revert path. While the façade exists, a structural PR can revert to the prior
implementation behind the same imports and HTTP contract. The façade-removal PR
is reverted as a unit if an integration dependency is found.

Operational PR 3 may add migration 0020 for certified-profile provenance. Before
any migration after 0019 is applied, the release must choose either a
backward-compatible maintenance path or a verified pre-upgrade database snapshot
and rollback procedure. A schema-ahead database must not be served directly by
v0.3.1. Rollback after such a migration follows the selected compatibility or
snapshot procedure, with migration checksums and worker compatibility verified;
it is not an unconditional old-wheel redeploy.

## Risks

- Moving transaction boundaries can change locking duration and deadlock
  behavior even when SQL statements are equivalent.
- An incomplete scheduling move can split transition ownership and reintroduce
  late-replay or reclaim bugs.
- The explicit job-summary refresh exception can be accidentally hidden inside
  a query again.
- Removing the façade can break downstream Python imports not visible in this
  repository.
- Strong static rules can encourage indirection without improving ownership if
  exceptions are too broad.

Mitigations are small PRs, contract goldens, real PostgreSQL contention tests,
symbol inventories, and reversible adapters.

## Open Questions

- Should the explicit summary refresh remain on the existing route or become a
  separately observable application action while preserving wire behavior?
- Which state-transition exceptions, if any, are required for migration and
  bootstrap code?
- What deprecation notice is sufficient for external users of the service
  façade before v0.4?
- Should read-only capacity queries share diagnostics infrastructure or remain
  a dedicated application query?
- Which lock-duration and deadlock thresholds should block a structural PR?
