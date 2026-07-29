# Control Service Façade Migration

English | [中文](control-facade-migration.zh-CN.md)

`ocr_platform.control.service` was a v0.3 compatibility façade and is removed
in v0.4.0. HTTP is the stable Control interface. Python integrations must use
the explicit command, query, schema, or configuration owner listed below.
Domain `core`, policy, scheduling-private names, constants, and test helpers
remain internal and are not compatibility promises.

Removing `ocr_platform.control.service` is the only intentional Python import
breaking change in v0.4.

The repository migration is complete: the former module no longer exists, all
repository consumers have moved, and CI rejects direct, relative, dynamic, or
embedded imports and string monkeypatch targets for the old path.

## Consumed-symbol migration

| Old symbol | Repository replacement |
| --- | --- |
| `create_job` | `ocr_platform.control.domains.jobs.commands.create_job` |
| `upsert_model_profile` | `ocr_platform.control.domains.model_profiles.commands.upsert_model_profile` |
| `ShardAttemptConflictError` | `ocr_platform.control.domains.manifests.commands.ShardAttemptConflictError` |
| `claim_next_pending_shard` | `ocr_platform.control.domains.manifests.commands.claim_next_pending_shard` |
| `claim_next_scan_unit` | `ocr_platform.control.domains.manifests.commands.claim_next_scan_unit` |
| `complete_scan_unit` | `ocr_platform.control.domains.manifests.commands.complete_scan_unit` |
| `update_work_shard` | `ocr_platform.control.domains.manifests.commands.update_work_shard` |
| `database` | `ocr_platform.control.database` |
| `_database_migration_preflight_issue` | `ocr_platform.control.domains.workers.preflight.database_migration_preflight_issue` |
| `_claimable_scan_unit_id_select` | `ocr_platform.control.scheduling._claimable_scan_unit_id_select` (repository test only; internal) |
| `_claimable_shard_id_select` | `ocr_platform.control.scheduling._claimable_shard_id_select` (repository test only; internal) |
| `_manifest_for_scan_unit_completion_select` | `ocr_platform.control.domains.manifests.ports.manifest_for_scan_unit_completion_select` |
| `infer_failure_category` | `ocr_parser.infra.failure_category.infer_failure_category` |
| `JOB_FILE_DETAIL_LIMIT` | inject `ocr_platform.control.limits.ControlLimits.job_file_detail_limit` |
| `JOB_EVENT_DETAIL_LIMIT` | inject `ocr_platform.control.limits.ControlLimits.job_event_detail_limit` |
| `JOB_LOG_DETAIL_LIMIT` | inject `ocr_platform.control.limits.ControlLimits.job_log_detail_limit` |
| `POOL_SERVER_ID` | no public replacement; repository tests use its owner in `domains.common` |
| `SCAN_UNIT_CLAIM_BATCH_SIZE` | no public replacement; tests patch the owning manifest use case |
| `SERVER_STALE_AFTER_SECONDS` | no public replacement; tests configure or patch worker identity policy |
| `SHARD_LEASE_SECONDS` | no public replacement; configuration tests read `domains.common` |
| `STALE_AFTER_SECONDS` | no public replacement; configuration tests read `domains.common` |
| `json_loads_object` | standard-library `json.loads` |
| `utcnow` | inject a clock; repository tests use `ocr_platform.control.models.utcnow` |
| `__all__` | removed with the façade; no replacement |

The complete machine-checked mapping is stored in
`tests/fixtures/contracts/control_facade_inventory.json`.

## Upgrade action

Search application code for `ocr_platform.control.service` before upgrading.
Replace each import with its owner. There is intentionally no deprecation shim
or wildcard re-export in v0.4.0; importing the old module raises
`ModuleNotFoundError`.
