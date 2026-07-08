# OCR Platform 生产备份与恢复 Runbook

本文档面向千万级 PDF 生产任务。平台的状态分成三类：

- PostgreSQL 控制面状态：job、worker、manifest 元数据、shard 元数据、
  lease、attempt、聚合计数、model profile 等。
- 共享盘任务快照：`manifest_root` 下的 JSONL manifest、scan-unit manifest、
  shard JSONL 文件和 freeze report 相关文件。
- OCR 输出目录：每个 PDF 的 Markdown/JSON/PDF 等产物，以及 `.ocr_status.json`
  sidecar。

生产备份必须覆盖 PostgreSQL 和共享盘上的 manifest/shard 文件。OCR 输出目录是否
纳入同一备份策略，取决于业务是否允许重跑 OCR。

## 备份原则

PostgreSQL 是调度状态的事实来源，但它不保存千万级 PDF 全量清单内容；全量清单
在 `manifest_root` 的 JSONL manifest 和 shard 文件中。因此只备份 PG 不够，只
备份共享盘文件也不够。

建议最低策略：

- 每日全量 `pg_dump`，并保留 WAL/PITR 能力。
- 每次创建大任务后，备份对应 job 的 `manifest_root/<job_id>/`。
- 灰度和生产批次完成后，备份 freeze report、manifest/shard JSONL 与 OCR 输出。
- 对 `/etc/ocr-platform/control.env` 只备份加密版本或由 secret manager 管理，
  不要把明文 API token、model profile API key 放进普通备份包。

生产中若设置 `OCR_JOB_FILE_DETAIL_LIMIT=0` 或 `OCR_JOB_EVENT_DETAIL_LIMIT=0`，
`job_files` 和 `job_events` 不再保存全量明细，这是预期行为。恢复与审计应以
PG 聚合状态、shard 状态、sidecar、manifest integrity 为准。

## PostgreSQL 备份

备份前记录 schema migration 状态：

```bash
psql "$OCR_PLATFORM_DATABASE_URL" \
  -c "select version, applied_at from schema_migrations order by version;"
```

逻辑备份示例：

```bash
mkdir -p /backup/ocr-platform/postgres
pg_dump "$OCR_PLATFORM_DATABASE_URL" \
  --format=custom \
  --file "/backup/ocr-platform/postgres/ocr_platform-$(date +%Y%m%d-%H%M%S).dump"
```

建议同时由 DBA 配置 PostgreSQL WAL 归档或云厂商 PITR。`pg_dump` 适合演练和
跨环境恢复，PITR 适合误操作和故障点恢复。

## Manifest 与 Shard 文件备份

`manifest_root` 通常位于共享盘，例如：

```text
/shared/.ocr_platform/manifests/<job_id>/
```

目录内通常包含：

- 根 manifest：JSONL manifest 或分布式扫描聚合路径。
- scan-unit manifest：每个 scan unit 扫描到的 PDF JSONL。
- shard JSONL：worker 实际领取执行的文件列表。
- meta 文件：文件数、字节数、扫描参数等辅助信息。

备份示例：

```bash
JOB_ID=<job-id>
MANIFEST_ROOT=/shared/.ocr_platform/manifests
mkdir -p /backup/ocr-platform/manifests
rsync -a --delete \
  "$MANIFEST_ROOT/$JOB_ID/" \
  "/backup/ocr-platform/manifests/$JOB_ID/"
```

如果共享盘本身已有快照能力，可以使用存储快照，但仍建议对关键 job 的
`manifest_root/<job_id>/` 做独立可见备份，方便恢复时快速比对。

## OCR 输出与 Sidecar

OCR 输出目录由 `output_dir + relative_path` 决定。每个 PDF 应有
`.ocr_status.json` sidecar，记录成功、失败、页数、耗时、模型配置摘要和错误
类型。

如果业务要求恢复后不重复跑已成功 PDF，输出目录和 sidecar 必须一起备份：

```bash
JOB_ID=<job-id>
rsync -a --delete \
  /shared/ocr-output/$JOB_ID/ \
  /backup/ocr-platform/output/$JOB_ID/
```

如果业务允许重跑 OCR，输出目录可以不作为强一致备份对象，但恢复后要确认
`force_reprocess=false`，让 parser 根据 sidecar/artifact completeness 跳过已完成
文件。

恢复或灰度后，可以直接用 manifest/shard JSONL 审计输出完整性：

```bash
cd /opt/ocr-platform/ocrparser
python3 tools/audit_manifest_outputs.py \
  --manifest /shared/.ocr_platform/manifests/<job-id>/shards/shard-000001.jsonl \
  --output-dir /shared/ocr-output/<job-id> \
  --check-input
```

报告中的 `issues_by_category` 会聚合 `sidecar_missing`、`artifact_missing`、
`artifact_invalid`、`input_missing`、`input_changed` 等问题，并只保留有限
`issue_samples`，避免为了审计把千万级文件明细重新写进 PG。

## 恢复流程

1. 停止 control 和所有 agent，避免恢复过程中继续 claim shard。
2. 恢复 PostgreSQL：

```bash
createdb ocr_platform_restore
pg_restore \
  --dbname "postgresql+psycopg://<user>:<password>@<host>:5432/ocr_platform_restore" \
  /backup/ocr-platform/postgres/<dump-file>.dump
```

如果使用 `psql` 原生连接串而不是 SQLAlchemy DSN，请按 DBA 标准命令替换。

3. 确认 `schema_migrations`：

```bash
psql "$OCR_PLATFORM_DATABASE_URL" \
  -c "select version, applied_at from schema_migrations order by version;"
```

4. 恢复 manifest/shard 文件：

```bash
JOB_ID=<job-id>
MANIFEST_ROOT=/shared/.ocr_platform/manifests
rsync -a --delete \
  "/backup/ocr-platform/manifests/$JOB_ID/" \
  "$MANIFEST_ROOT/$JOB_ID/"
```

5. 恢复 OCR 输出目录（如果该批次要求避免重复 OCR）。
6. 启动 control，但先不要启动 agent。
7. 对关键 job 做一致性校验：

```bash
curl -H "Authorization: Bearer $OCR_PLATFORM_API_TOKEN" \
  "http://<control-host>:8080/api/jobs/{job_id}/manifest/integrity"

curl -H "Authorization: Bearer $OCR_PLATFORM_API_TOKEN" \
  "http://<control-host>:8080/api/jobs/{job_id}/manifest/freeze-report"
```

`/api/jobs/{job_id}/manifest/integrity` 应返回 `ok=true`。如果 manifest 文件存在
但 shard file_count 与 DB 不一致，不要启动 agent；先恢复正确的 manifest/shard
文件或重新生成快照。

`/api/jobs/{job_id}/manifest/freeze-report` 用来确认扫描是否已经冻结。恢复后如果
freeze report 显示扫描仍在进行，需要确认是否有 pending/running scan unit，再决定
是继续扫描还是废弃 job 重建。

8. 启动少量 agent，观察 heartbeat、claim 和 progress；确认正常后再恢复全部
worker。

## 可重建与不可重建

可重建：

- 未开始执行的 folder snapshot 可以重新扫描生成 JSONL manifest。
- 如果原始 PDF 输入目录未变，manifest 可以重新生成，但 shard 编号和 job 历史不
一定与原 job 完全一致。
- OCR 输出在业务允许重跑时可重建。

不可轻易重建：

- 已经进入生产执行的 job 的 PG 调度状态。
- 已 freeze 的 manifest/shard 文件，尤其是已被 worker 领取过的 shard。
- `.ocr_status.json` 和输出 artifact，如果业务依赖跳过已成功 PDF。

因此，生产故障恢复优先使用 PG dump + `manifest_root` 备份恢复原 job；只有在确认
任务尚未正式执行或可接受重复 OCR 时，才选择重扫目录并创建新 job。

## 恢复后验收清单

- `schema_migrations` 包含当前 release 要求的所有版本。
- 控制端 `/ui/` 可访问，Workers 中没有版本不一致警告。
- 关键 job 的 `/api/jobs/{job_id}/manifest/integrity` 为 `ok=true`。
- 关键 job 的 `/api/jobs/{job_id}/manifest/freeze-report` 与预期一致。
- `manifest_root/<job_id>/` 中 JSONL manifest 和 shard 文件存在。
- 输出目录存在 `.ocr_status.json`，并且 artifact completeness 检查不会把半成品当
  成成功。
- 小规模启动 agent 后，claim 没有重复，stale/attempt-aware 防护正常。

## 定期演练

建议每月至少演练一次：

1. 使用最近一次 `pg_dump` 恢复到临时 PostgreSQL。
2. 使用 `rsync` 恢复一个已完成 job 的 `manifest_root/<job_id>/`。
3. 启动临时 control，执行 manifest integrity 和 freeze report 检查。
4. 对临时库运行：

```bash
python tools/pg_claim_stress.py \
  --database-url "$OCR_PLATFORM_DATABASE_URL" \
  --shards 1000 \
  --scan-units 1000 \
  --workers 64 \
  --json
```

验收标准：`ok=true`、`duplicate_claims={}`、`missing_claims=0`、
`attempt_conflict_rejected=true`、`scan_unit_claims.ok=true`。
