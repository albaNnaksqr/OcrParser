# OcrParser v0.4.0 Execution Plan

English | [中文](rfc-v0.4-execution-plan.zh-CN.md)

Status: Approved plan, frozen for documentation and review during the v0.3.1
observation window. Runtime implementation must not start before
2026-08-02 14:51:07+08:00.

This document turns the [v0.4 Operational Maturity RFC](rfc-v0.4.md) and the
[Control Internals RFC](rfc-v0.4-control-internals.md) into an ordered delivery
plan. The module boundaries and current audit snapshot are recorded in the
[Control Module Map](control-module-map.md).

## Outcome

v0.4.0 keeps Control as a modular monolith and makes it the authoritative
control plane:

- application use cases own cross-domain coordination and transactions;
- domain policies own state transitions;
- an internal, non-HTTP `scheduling` domain owns claim, lease, attempt, retry,
  fencing, and recovery;
- explicit operations, certified engine profiles, bounded observability,
  capacity guidance, and audit evidence become production features;
- existing HTTP, database, CLI, manifest, output, and state-value contracts
  remain compatible.

The only intentional public breaking change is removal of the
`ocr_platform.control.service` Python compatibility façade at the end of v0.4.

## Execution Governance

The public repository `main` branch is the only source line. Work uses one
shared checkout and a strict single-writer rule.

| Role | Responsibility |
| --- | --- |
| Main Agent | Dispatch, diff review, evidence verification, acceptance, and release authorization only; it does not edit, stage, commit, push, tag, or publish |
| `migration_bridge_agent` | v0.3.2 migration bridge and migration-first PostgreSQL initialization |
| `contract_guardian` | Complete compatibility goldens, dependency gates, and façade inventory |
| `operations_builder` | Migration/readiness policy, certification, observability, capacity, and audit |
| `control_refactorer` | Explicit transactions, scheduling kernel, domain ownership split, and façade migration |
| `validation_operator` | Isolated PostgreSQL, recovery, package, soak, and engine-integration verification; no repository writes |
| `release_integrator` | Versioning, bilingual release documentation, clean wheel, tag, and GitHub Release after authorization |

Only one agent may modify tracked files at a time. Validation may run in
parallel only in isolated task directories and must not change repository
state. Every code wave ends with Main Agent review, targeted tests, full CI,
and a clean working tree before the next writer starts.

If the observation period finds a new P0/P1 defect, v0.4 work freezes and the
defect returns to a v0.3.x maintenance release.

## Delivery Sequence

### Wave 0: v0.3.2 schema bridge

Release v0.3.2 before v0.4 runtime development.

- Add the additive `0020_model_profile_certification.sql` migration and its
  centralized ORM mapping.
- Define an optional one-to-one `model_profile_certifications` record for a
  `ModelProfile`, with: `profile_id`; `enforcement` (`off`, `verified`, or
  `certified`); `status` (`contract_only`, `verified`, `certified`, or
  `blocked`); parser, model, runtime, and optional layout revisions/digests;
  fixture-set and evidence digests; `certified_at`; `risk_acceptance_json`; and
  `updated_at`. v0.3.2 does not insert records for existing profiles
  automatically.
- Do not store keys, credentials, internal endpoints, private paths, customer
  documents, or OCR text in certification records.
- Do not expose certification through HTTP or enforce it in v0.3.2. Missing
  records have no behavioral effect.
- Make PostgreSQL initialization migration-first. The current
  `create_all()`-before-migrations path is a release blocker because a fresh
  database can acquire the latest ORM schema before the migration catalog
  establishes its history. PostgreSQL must run the checksum-verified migration
  catalog as the schema authority; direct SQLite development may retain its
  create-all convenience path.
- Verify fresh PostgreSQL installation, v0.3.1-to-v0.3.2 upgrade, concurrent
  advisory locking, checksums, and v0.3.2 operation against schema 0020.

v0.3.2 is the rollback floor after migration 0020 is applied. A database that
recognizes 0020 may roll back to v0.3.2 after compatibility verification, but
must not be served directly by v0.3.1. Destructive downgrade is not supported;
a downgrade below v0.3.2 requires a verified pre-upgrade snapshot restore.
The whole v0.4.0 line adds no migration 0021 and never changes the checksum of
0020. If another schema change becomes necessary, it requires a separate
compatible maintenance bridge release and a new review of the rollback floor
before v0.4 work resumes.

### PR 1: complete contracts and decreasing static gates

- Freeze canonical OpenAPI JSON, request/response schemas, status codes, error
  bodies, database metadata, migration checksums, state values, and
  claim/attempt behavior.
- Capture the PR 1 fixtures again from the exact v0.3.2 commit containing
  migration 0020. The pre-Wave-0 audit below is evidence, not the final gate.
- Store violation inventories by exact site and symbol. A later PR may remove
  entries but may not replace them with new sites while keeping the count at
  or below the baseline.
- Generate and test a symbol-level migration map for
  `ocr_platform.control.service`.
- Cover the complete HTTP behavior matrix, including expected 400, 401, 403,
  404, 409, 422, and 503 responses.

The pre-Wave-0 audit at commit `6b7cff3` recorded:

| Surface | Audit value |
| --- | --- |
| OpenAPI | 47 paths, 49 operations, 56 schemas; canonical SHA-256 `2217e4551be81570540c406d501a2c1d23aba15fca31f9d933c6434abc0b76ad` |
| Database | 11 tables, 171 columns, 15 ORM indexes, 11 foreign keys; migrations 0001-0019 |
| Cross-domain core dependencies | 42 sites / 44 symbols, including 17 private dependencies and 37 lazy wrappers |
| Explicit transaction calls | 50 total: 29 commit, 7 rollback, 14 flush |
| Semantic read-side mutations | 4 allowlisted sites: 3 Job-summary queries and the Profile list |
| Status-like writes | 40 sites |
| Compatibility façade | 286 exports; 7 real consumer files; 19 AST import sites plus 1 embedded import; 21 monkeypatch sites; 23 unique consumed symbols |

These values must not be copied into the final PR 1 fixture. PR 1 regenerates
the complete baseline after v0.3.2/0020 lands, then uses subset-decreasing site
inventories until the target is zero. This PR is otherwise characterization
only and does not change runtime behavior.

### PR 2: explicit migration, readiness, and bootstrap

- Introduce immutable `ControlSettings` and explicit construction of the
  session factory, remote executor, limits, authentication, and runtime mode.
- Add `OCR_PLATFORM_AUTO_MIGRATE`. PostgreSQL defaults to off when unset and
  upgrades only when explicitly set to `1`; direct SQLite development retains
  its documented convenience path.
- Keep `/healthz` as process health. When PostgreSQL schema is behind or has a
  checksum mismatch, `/readyz` returns `503` with actionable
  `ocr-platform-migrate plan|apply|verify` guidance.
- While unready, return a stable `503` from business APIs while retaining the
  diagnostics, database, source-offer, and legal surfaces needed for recovery.
- Retain `OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS` for one release as the
  strict fail-start compatibility switch.
- Move default ModelProfile creation to explicit bootstrap. Profile list
  queries become read-only.

### PR 3: certified engine profiles

- Add an optional `certification` object to ModelProfile request and response
  contracts without changing existing fields.
- Treat legacy profiles without a certification row as
  `status=contract_only` and `enforcement=off`; they continue to run.
- A Certified profile requires parser and model revisions, an immutable runtime
  digest, fixture-set digest, evidence digest, and layout provenance when
  applicable.
- A Verified profile under enforcement requires a named, timestamped,
  reasoned risk acceptance.
- Add optional Agent engine-provenance file configuration. Heartbeats report
  only revisions and digests, never endpoints or credentials.
- Use one certification policy for Job preflight and creation:
  `off` is informational, `verified` accepts Verified or Certified, and
  `certified` requires an exact profile/Agent/build match.
- Return stable errors:
  `model_profile_certification_missing`,
  `model_profile_certification_mismatch`, and
  `model_profile_risk_acceptance_required`.

No existing engine is automatically promoted to Certified.

### PR 4: observability, capacity, and audit

- Add token-protected `/api/system/metrics` in Prometheus text format.
- Bound labels to enumerated engine, stage, status, and failure/fallback
  categories. Never use job IDs, paths, free-form errors, credentials, or
  document content as labels.
- Extend diagnostics compatibly with optional `capacity`, `audit`, and `alerts`
  objects.
- Capacity reports ready/available worker slots, pending shard depth, observed
  pages per hour over the last 60 minutes, estimated drain time, confidence
  (`none`, `low`, or `medium`), and fixed recommendation codes. Fewer than ten
  observed pages produces no ETA.
- Provide operator-owned alert templates for migration drift, heartbeat age,
  stale leases, spool backlog, stage failure, fallback, and artifact audit.
- Reuse existing JobEvent, ShardAttempt, manifest-integrity, and output-audit
  evidence without recording OCR content. Capacity remains advisory and never
  autoscales workers or model services.

### PR 5: transaction and dependency foundation

- Complete explicit dependency injection and remove mutable module-level
  registries.
- Give each application command one `session.begin()` boundary: one commit on
  success and one rollback on failure. Leaf mutations may flush but never
  commit or roll back; queries perform no DML.
- Migrate lower-risk operations first: Profile CRUD, Job events/logs/counters,
  and Manifest registration.
- Preserve Job-summary wire behavior through a visible
  `refresh_job_summary` application call followed by a read-only query. Do not
  hide mutation inside a query or HTTP adapter.

### PR 6: scheduling kernel

- Add an internal `scheduling` domain with no router or public compatibility
  API.
- Make it the sole policy owner for ScanUnit, WorkShard, and ShardAttempt
  transitions, claim ordering, lease renew/expire/reclaim, attempt numbering,
  retry, server/attempt fencing, terminal replay, stop, and recovery.
- Coordinate Worker registration/heartbeat, Job stop, and shard updates through
  application use cases.
- Preserve current `FOR UPDATE/SKIP LOCKED` behavior, claim order, lease
  windows, attempt uniqueness, and terminal-state monotonicity.

This PR changes ownership, not the HTTP contract, database schema, or scheduling
algorithm.

### PR 7a-7c: domain ownership split

- **PR 7a — jobs:** lifecycle, projections, events, logs, and counters.
- **PR 7b — manifests:** construction, path policy, freeze, and integrity.
- **PR 7c — workers:** identity, heartbeat, eligibility, and preflight.

Each PR is independently reversible and contains no migration, HTTP change, or
algorithm change. After PR 7c, private-core cross-domain imports, cycles, query
DML, leaf commit/rollback, and state assignments outside owner policies are all
zero.

### PR 8: façade removal

- Migrate every repository production import, test, tool, and documented
  integration using the symbol-level map.
- Remove wildcard exports, dynamic forwarding, and
  `ocr_platform.control.service`.
- Keep HTTP as the stable public interface. Explicit domain
  commands/queries/schemas are the supported Python integration surface;
  private core, policy, and scheduling paths are not compatibility APIs.

This is the only intentional public breaking change in v0.4.0.

### PR 9: release candidate and release

- Publish `0.4.0rc1` with bilingual upgrade guidance, the façade migration map,
  architecture documentation, and alert examples.
- Run the full automated gate, isolated multi-cycle recovery verification, and
  a four-hour mock soak. Repeat canonical integration fixtures against the
  supported real engines without comparing them to historical throughput.
- Observe the exact RC for 48 hours without a new P0/P1. After acceptance, the
  only tracked changes allowed for `0.4.0` are final version and release
  documentation.
- Build from a clean tag-matching commit with `release_build=true` and
  `dirty=false`, then publish the accepted wheel and GitHub Release.
- Observe the release for seven days. A P0/P1 pauses subsequent work and starts
  a scoped maintenance release.

Public reports contain sanitized outcomes and artifact digests, not private
infrastructure topology, endpoints, credentials, paths, or customer data.

## Stage Acceptance And Rollback

Each PR must be independently mergeable and must leave all characterization
gates at or below its accepted site inventory. Structural PRs do not include
schema, HTTP, or algorithm changes. A failed structural PR is reverted as one
commit while the façade still presents the previous contract.

Wave 0 and PRs 2-4 are operational changes and require an explicit rollback
record. After 0020:

- v0.3.2 is the oldest wheel allowed to operate that database;
- a v0.4 runtime rollback uses v0.3.2 only after migration, Worker, and
  certification compatibility checks;
- rollback below v0.3.2 restores a verified pre-0020 snapshot;
- a checksum mismatch or unknown migration blocks startup and rollback.

Migration 0020 is the schema ceiling for v0.4.0. Migration 0021 and any change
to the 0020 checksum are forbidden; a new schema requirement first returns to
a separately released compatibility bridge and rollback review.

Any tracked-file change after an RC validation invalidates that candidate and
requires rebuilding and repeating the affected gates.

## Compatibility And Acceptance

The following remain unchanged: console scripts, CLI arguments and exit codes,
existing HTTP paths/fields/statuses, Job/Shard/Attempt state values, manifest
JSONL, Markdown/JSON/sidecar outputs, directory layout, migrations 0001-0019
and their checksums, Parser top-level façades, and legacy fallback statuses
through at least v0.5.

Additive interfaces are migration 0020, optional profile certification,
`OCR_PLATFORM_AUTO_MIGRATE`, optional Agent provenance configuration,
`/api/system/metrics`, and optional diagnostics capacity/audit/alerts fields.
The sole breaking interface is removal of `ocr_platform.control.service`.

Release gates include:

- all existing tests plus new characterization and policy tests on Python
  3.10-3.12;
- base/platform/s3/layout/full clean-wheel installation and all console
  scripts;
- PostgreSQL fresh install, 0.3.1-to-v0.3.2-to-v0.4.0 upgrade, migration lock
  and checksum verification, and v0.4.0-to-v0.3.2 compatibility rollback;
- concurrent claim, lease, reclaim, late replay, attempt fencing, Control
  interruption, spool replay, and Agent restart;
- certification missing/mismatch/risk-acceptance/legacy-profile cases;
- bounded-label and secret-redaction checks, authentication, Remote Admin,
  AGPL `/source`, UI/package data, and wheel provenance;
- no more than 10% regression in claim stress, Job-summary, and mock end-to-end
  baselines.

Data loss, duplicate claim/artifact, incorrect migration, stuck work, replay
failure, post-shutdown reporting, authentication/source-offer defect, or
sustained resource leak blocks release.

## Fixed Constraints

- This plan means v0.4.0, not v4.0.0.
- The public repository is the only source line; no separate implementation
  checkout is created.
- Control remains one deployable modular monolith.
- No microservices, Alembic, async database conversion, generic Repository
  framework, or new frontend framework.
- OCR, layout, table, Markdown, and scheduling algorithms do not change.
- Engine quality limitations may remain explicitly Verified; integration,
  consistency, migration, and recovery defects block release.
