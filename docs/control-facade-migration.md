# Control Service Façade Migration

English | [中文](control-facade-migration.zh-CN.md)

`ocr_platform.control.service` is a temporary compatibility façade and is
removed in v0.4.0. This document records repository-visible consumers; it does
not declare every runtime export to be a supported Python API.

The reviewed baseline contains 286 runtime export names, 19 direct AST import
statements, one embedded subprocess import, no dynamic imports, 21 monkeypatch
sites, seven test/tool consumer files, and no production consumer. Those sites
consume 23 unique symbols. The complete 286-symbol classification, origin
evidence, targets, and stable site fingerprints are stored in
`tests/fixtures/contracts/control_facade_inventory.json`.

## Migration rules

- `supported_explicit_target`: migrate to the named existing module before
  façade removal.
- `settings_pending`: migrate in PR 2 to immutable `ControlSettings`; tests
  inject settings instead of monkeypatching module globals.
- `scheduling_application_pending`: wait for the named application or
  scheduling owner, then migrate in the stated wave.
- `internal_no_compat`: stop using the symbol as an integration surface. No
  compatibility replacement is promised.
- `unsupported_leaked`: an incidental wildcard export, including SQLAlchemy,
  typing, datetime, pathlib, parser, and manifest dependencies. It is not a
  Control API.

The gate is deletion-only. Runtime export symbol keys and stable
`(site ID, AST/string fingerprint)` pairs may disappear, but new or replaced
exports, direct/wildcard/dynamic/embedded imports, symbol uses, or monkeypatch
sites fail CI. Production consumers always fail, even if a matching test-site
baseline exists.

## Repository-consumed symbols

| Old façade symbol | Classification | Replacement or planned owner | Wave |
| --- | --- | --- | --- |
| `database` | supported explicit target | `ocr_platform.control.database` | PR 8 |
| `ShardAttemptConflictError` | supported explicit target | `ocr_platform.control.domains.manifests.commands.ShardAttemptConflictError` | PR 8 |
| `claim_next_pending_shard` | supported explicit target | `ocr_platform.control.domains.manifests.commands.claim_next_pending_shard` | PR 8 |
| `claim_next_scan_unit` | supported explicit target | `ocr_platform.control.domains.manifests.commands.claim_next_scan_unit` | PR 8 |
| `complete_scan_unit` | supported explicit target | `ocr_platform.control.domains.manifests.commands.complete_scan_unit` | PR 8 |
| `update_work_shard` | supported explicit target | `ocr_platform.control.domains.manifests.commands.update_work_shard` | PR 8 |
| `create_job` | supported explicit target | `ocr_platform.control.domains.jobs.commands.create_job` | PR 8 |
| `infer_failure_category` | supported explicit target | `ocr_parser.infra.failure_category.infer_failure_category` | PR 8 |
| `upsert_model_profile` | supported explicit target | `ocr_platform.control.domains.model_profiles.commands.upsert_model_profile` | PR 8 |
| `STALE_AFTER_SECONDS` | settings pending | planned `ControlSettings.job_stale_after_seconds` | PR 2 |
| `SERVER_STALE_AFTER_SECONDS` | settings pending | planned `ControlSettings.server_stale_after_seconds` | PR 2 |
| `SHARD_LEASE_SECONDS` | settings pending | planned `ControlSettings.shard_lease_seconds` | PR 2 |
| `JOB_FILE_DETAIL_LIMIT` | settings pending | planned `ControlSettings.job_file_detail_limit` | PR 2 |
| `JOB_EVENT_DETAIL_LIMIT` | settings pending | planned `ControlSettings.job_event_detail_limit` | PR 2 |
| `JOB_LOG_DETAIL_LIMIT` | settings pending | planned `ControlSettings.job_log_detail_limit` | PR 2 |
| `SCAN_UNIT_CLAIM_BATCH_SIZE` | settings pending | planned `ControlSettings.scan_unit_claim_batch_size` | PR 2 |
| `_claimable_scan_unit_id_select` | scheduling/application pending | planned `scheduling.queries.claimable_scan_unit_id_select` | PR 6 |
| `_claimable_shard_id_select` | scheduling/application pending | planned `scheduling.queries.claimable_shard_id_select` | PR 6 |
| `_manifest_for_scan_unit_completion_select` | scheduling/application pending | planned `scheduling.queries.manifest_for_scan_unit_completion_select` | PR 6 |
| `_database_migration_preflight_issue` | scheduling/application pending | planned `application.diagnostics.database_migration_preflight_issue` | PR 2 |
| `POOL_SERVER_ID` | internal, no compatibility | stop using the scheduler sentinel as integration API | PR 8 |
| `json_loads_object` | internal, no compatibility | stop using the internal JSON helper as integration API | PR 8 |
| `utcnow` | internal, no compatibility | inject a clock or use an owning internal helper in tests | PR 8 |

The consumed-symbol split is therefore 9 existing targets, 7 settings
migrations, 4 scheduling/application owners, and 3 integrations that must
stop. The façade stays intact until every listed consumer has been migrated
and the corresponding inventory reaches zero.
