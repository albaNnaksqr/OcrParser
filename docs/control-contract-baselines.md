# Control Contract Baselines

English | [中文](control-contract-baselines.zh-CN.md)

The v0.4 Control refactor uses reviewed, checked-in fixtures to distinguish an
internal move from an external behavior change. The current baseline records:

- the complete canonical OpenAPI document;
- representative HTTP success and error behavior captured through real
  `TestClient` requests, including authentication, Remote Admin, readiness,
  validation, not-found, bad-request, and conflict paths. Each scenario stores
  the complete JSON response body. Field normalization is allowed only for an
  explicitly named dynamic path, replacement, and reason recorded beside that
  scenario, and the target and replacement must both be scalar; validation-
  error arrays retain their order and every error entry;
- a machine-generated matrix for all 49 canonical OpenAPI operations. Each
  entry records declared statuses, AST-discovered router and explicitly called
  service status branches, behavior-scenario evidence, and a branch-specific
  source-backed reason for every branch not executed by the representative
  behavior set. Behavior evidence binds to the exact source branch; only the
  global API-token middleware and FastAPI request-validation handler may be
  shared. An independent scan of every `ocr_platform/control/**/*.py` runtime
  source must match the matrix's transport evidence, and domain
  `core.py`/`commands.py`/`queries.py` files may not import FastAPI or Starlette
  transport;
- scheduling behavior captured through real service calls for WorkShard and
  ScanUnit claim ordering, lease renewal/expiry/reclaim, attempt fencing,
  success/failure terminal replay, mixed-state stop, and recovery
  finalization. PostgreSQL concurrent `SKIP LOCKED` behavior remains an
  explicit external validation requirement; SQLite scenarios and SQL
  compilation are not presented as concurrency proof;
- ORM tables, columns, types, nullability, defaults, primary keys, foreign
  keys, indexes, and check constraints;
- indexes that SQLite actually creates, including automatic primary-key
  indexes reported by `PRAGMA index_list`; and
- 13 status surfaces with closed sets derived from database checks, Pydantic
  literals, domain constants, or AST-discovered transitions. Every closed
  value has source evidence and the fixture distinguishes real behavior
  observations from source-only coverage. ShardAttempt and manifest-freeze
  projections are tied to AST-verified source relationships rather than fixed
  line numbers. External/event strings stay open;
  notably a worker-provided `ManifestIntegrityResponse.status` is not treated
  as an exhaustive Control-owned enum; and
- site-level architecture debt for cross-domain core imports and graph SCCs,
  transaction calls, direct and semantic query mutations, and status writes
  outside owner policies. Stable IDs use
  `module:function:symbol-or-operation:ordinal`; line numbers are evidence
  only, and every site carries a normalized AST fingerprint. The query gate
  recognizes ORM attribute writes, SQLAlchemy bulk APIs, and DML passed through
  `execute`, `scalar`, or `scalars`; its function-aware semantic call graph
  follows module-level and lazy imports across all same-domain runtime modules,
  including future command and application modules; and
- a tombstone gate for the removed
  `ocr_platform.control.service` façade. The module must not exist and direct,
  relative, dynamic, embedded, or string monkeypatch references are forbidden.
  The 24 symbols formerly consumed by repository tests and tools have completed
  targets in `tests/fixtures/contracts/control_facade_inventory.json` and
  [Control façade migration](control-facade-migration.md).

Migration history is not copied into another fixture. The existing
`tests/fixtures/contracts/control_migration_checksums.json` file remains the
single source of migration versions and fixed-byte SHA-256 values. The database
metadata fixture records only a path and digest reference to it.

## Review Workflow

Tests compare generated data with the checked-in fixtures and never rewrite
them. A maintainer intentionally refreshes the files with:

```bash
python3 tools/control_contracts.py refresh
python3 tools/control_contracts.py check
python3 tools/control_architecture_debt.py refresh
python3 tools/control_architecture_debt.py check
python3 tools/control_facade_inventory.py refresh
python3 tools/control_facade_inventory.py check
python3 -m pytest -q tests/test_control_contracts.py tests/test_control_scheduling_contracts.py tests/test_control_architecture_debt.py tests/test_control_facade_inventory.py tests/test_v01_behavior_contract.py tests/test_migration_runner.py
```

Run `refresh` twice and require a byte-identical worktree after the second run.
Review the complete fixture diff. An unexpected OpenAPI, schema, status, or
migration-reference change blocks a structural refactor; do not accept it by
blindly refreshing the fixture.

The v0.1 route-path golden remains in place as a smaller independent guard.
The architecture-debt gate is deliberately decreasing: current
`(stable ID, AST fingerprint)` pairs must be a subset of the reviewed
baseline. Removing a site is allowed; adding a site, replacing its AST,
introducing a domain edge, or creating a new SCC fails. Compatibility-façade
exports, imports, embedded/dynamic consumers, symbol uses, and monkeypatch sites
follow the same deletion-only rule. Production façade consumers are forbidden.
