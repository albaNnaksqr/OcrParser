# Control Service Façade 迁移表

[English](control-facade-migration.md) | 中文

`ocr_platform.control.service` 是 v0.3 的临时兼容 façade，已在 v0.4.0
删除。Control 的稳定接口是 HTTP；Python 集成必须改用下表中的明确 command、
query、schema 或配置 owner。Domain `core`、policy、scheduling 私有名称、
常量和测试 helper 仍属于内部实现，不构成兼容承诺。

删除 `ocr_platform.control.service` 是 v0.4 唯一有意的 Python import breaking
change。

仓库内迁移已经完成：旧模块不再存在，所有仓库 consumer 均已迁走；CI 会拒绝
旧路径的 direct、relative、dynamic、embedded import 和字符串 monkeypatch。

## 已消费 symbol 的迁移结果

| 旧 symbol | 仓库内替代目标 |
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
| `_claimable_scan_unit_id_select` | `ocr_platform.control.scheduling._claimable_scan_unit_id_select`（仅仓库测试，内部接口） |
| `_claimable_shard_id_select` | `ocr_platform.control.scheduling._claimable_shard_id_select`（仅仓库测试，内部接口） |
| `_manifest_for_scan_unit_completion_select` | `ocr_platform.control.domains.manifests.ports.manifest_for_scan_unit_completion_select` |
| `infer_failure_category` | `ocr_parser.infra.failure_category.infer_failure_category` |
| `JOB_FILE_DETAIL_LIMIT` | 注入 `ocr_platform.control.limits.ControlLimits.job_file_detail_limit` |
| `JOB_EVENT_DETAIL_LIMIT` | 注入 `ocr_platform.control.limits.ControlLimits.job_event_detail_limit` |
| `JOB_LOG_DETAIL_LIMIT` | 注入 `ocr_platform.control.limits.ControlLimits.job_log_detail_limit` |
| `POOL_SERVER_ID` | 无公开替代；仓库测试从 `domains.common` owner 引用 |
| `SCAN_UNIT_CLAIM_BATCH_SIZE` | 无公开替代；测试 patch manifest use case owner |
| `SERVER_STALE_AFTER_SECONDS` | 无公开替代；测试配置或 patch worker identity policy |
| `SHARD_LEASE_SECONDS` | 无公开替代；配置测试读取 `domains.common` |
| `STALE_AFTER_SECONDS` | 无公开替代；配置测试读取 `domains.common` |
| `json_loads_object` | 标准库 `json.loads` |
| `utcnow` | 注入 clock；仓库测试使用 `ocr_platform.control.models.utcnow` |
| `__all__` | 随 façade 删除，无替代 |

完整、可机器校验的映射保存在
`tests/fixtures/contracts/control_facade_inventory.json`。

## 升级操作

升级前搜索应用代码中的 `ocr_platform.control.service`，并逐一替换为明确 owner。
v0.4.0 不提供弃用 shim 或 wildcard re-export；继续导入旧模块会得到
`ModuleNotFoundError`。
