# Control Service Façade 迁移表

[English](control-facade-migration.md) | 中文

`ocr_platform.control.service` 是临时兼容 façade，将在 v0.4.0 删除。本文只记录
仓库内可见 consumer，不会把全部运行时 export 声明为受支持的 Python API。

审查基线包含 286 个运行时 export 名称、19 个直接 AST import statement、1 个
embedded subprocess import、0 个 dynamic import、21 个 monkeypatch site、7 个
test/tool consumer 文件，且 production consumer 为 0。这些 site 共消费 23 个
unique symbol。完整 286 symbol 分类、来源证据、目标和 stable site fingerprint
保存在 `tests/fixtures/contracts/control_facade_inventory.json`。

## 迁移规则

- `supported_explicit_target`：删除 façade 前迁移到表中已有明确模块。
- `settings_pending`：在 PR 2 迁移到不可变 `ControlSettings`；测试通过注入 settings
  替代 monkeypatch module global。
- `scheduling_application_pending`：等待明确 application 或 scheduling owner 后，
  在指定波次迁移。
- `internal_no_compat`：停止把该 symbol 当作集成接口，不承诺兼容替代。
- `unsupported_leaked`：由 wildcard 意外泄漏的依赖，包括 SQLAlchemy、typing、
  datetime、pathlib、Parser 与 manifest 依赖；它们不是 Control API。

门禁只减不增。运行时 export symbol key 与 stable
`(site ID, AST/string fingerprint)` 可以删除；新增或替换 export、
direct/wildcard/dynamic/embedded import、symbol use 或 monkeypatch site 都会使
CI 失败。production consumer 无条件失败，即使 test site 基线中存在相似条目。

## 仓库实际消费的 symbol

| 旧 façade symbol | 分类 | 替代目标或计划 owner | 波次 |
| --- | --- | --- | --- |
| `database` | 已有明确目标 | `ocr_platform.control.database` | PR 8 |
| `ShardAttemptConflictError` | 已有明确目标 | `ocr_platform.control.domains.manifests.commands.ShardAttemptConflictError` | PR 8 |
| `claim_next_pending_shard` | 已有明确目标 | `ocr_platform.control.domains.manifests.commands.claim_next_pending_shard` | PR 8 |
| `claim_next_scan_unit` | 已有明确目标 | `ocr_platform.control.domains.manifests.commands.claim_next_scan_unit` | PR 8 |
| `complete_scan_unit` | 已有明确目标 | `ocr_platform.control.domains.manifests.commands.complete_scan_unit` | PR 8 |
| `update_work_shard` | 已有明确目标 | `ocr_platform.control.domains.manifests.commands.update_work_shard` | PR 8 |
| `create_job` | 已有明确目标 | `ocr_platform.control.domains.jobs.commands.create_job` | PR 8 |
| `infer_failure_category` | 已有明确目标 | `ocr_parser.infra.failure_category.infer_failure_category` | PR 8 |
| `upsert_model_profile` | 已有明确目标 | `ocr_platform.control.domains.model_profiles.commands.upsert_model_profile` | PR 8 |
| `STALE_AFTER_SECONDS` | settings pending | 计划 `ControlSettings.job_stale_after_seconds` | PR 2 |
| `SERVER_STALE_AFTER_SECONDS` | settings pending | 计划 `ControlSettings.server_stale_after_seconds` | PR 2 |
| `SHARD_LEASE_SECONDS` | settings pending | 计划 `ControlSettings.shard_lease_seconds` | PR 2 |
| `JOB_FILE_DETAIL_LIMIT` | settings pending | 计划 `ControlSettings.job_file_detail_limit` | PR 2 |
| `JOB_EVENT_DETAIL_LIMIT` | settings pending | 计划 `ControlSettings.job_event_detail_limit` | PR 2 |
| `JOB_LOG_DETAIL_LIMIT` | settings pending | 计划 `ControlSettings.job_log_detail_limit` | PR 2 |
| `SCAN_UNIT_CLAIM_BATCH_SIZE` | settings pending | 计划 `ControlSettings.scan_unit_claim_batch_size` | PR 2 |
| `_claimable_scan_unit_id_select` | scheduling/application pending | 计划 `scheduling.queries.claimable_scan_unit_id_select` | PR 6 |
| `_claimable_shard_id_select` | scheduling/application pending | 计划 `scheduling.queries.claimable_shard_id_select` | PR 6 |
| `_manifest_for_scan_unit_completion_select` | scheduling/application pending | 计划 `scheduling.queries.manifest_for_scan_unit_completion_select` | PR 6 |
| `_database_migration_preflight_issue` | scheduling/application pending | 计划 `application.diagnostics.database_migration_preflight_issue` | PR 2 |
| `POOL_SERVER_ID` | internal，不兼容 | 停止把 scheduler sentinel 当作集成 API | PR 8 |
| `json_loads_object` | internal，不兼容 | 停止把内部 JSON helper 当作集成 API | PR 8 |
| `utcnow` | internal，不兼容 | 测试注入 clock 或直接使用所属内部 helper | PR 8 |

因此 23 个 consumed symbol 的拆分为：9 个已有目标、7 个 settings 迁移、4 个
scheduling/application owner，以及 3 个必须停止的集成用法。在所有 consumer
完成迁移且对应 inventory 归零之前，façade 保持不变。
