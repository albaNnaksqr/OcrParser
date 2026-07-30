# 升级到 v0.4

[English](migration-v0.4.md) | 中文

本文适用于 `0.4.0rc2` 候选版本。生产部署前必须在隔离环境验证准确 wheel。公开
仓库继续作为唯一源码主线。

## 兼容范围

v0.4 保持：

- 现有 console script、CLI 参数和退出码；
- HTTP 路径、已有字段、状态码和 OpenAPI 行为；
- 数据库 schema 与截至 `0020_model_profile_certification` 的 migration 历史；
- Job、Shard、Attempt、manifest、输出和 fallback wire format；
- Parser 算法和顶层 Parser 兼容导入。

删除 `ocr_platform.control.service` 是 v0.4 唯一有意的 Python import breaking
change。自定义 Python 集成必须在升级前按照
[symbol 迁移表](control-facade-migration.zh-CN.md)迁移。

## 升级前

1. 备份 PostgreSQL，并记录当前部署 wheel、source revision 和 migration 状态。
2. 确认数据库没有 checksum mismatch 或未知 migration：

   ```bash
   ocr-platform-migrate status --database-url "$OCR_PLATFORM_DATABASE_URL"
   ocr-platform-migrate plan --database-url "$OCR_PLATFORM_DATABASE_URL"
   ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
   ```

3. 确认 Control/Agent 配置没有把凭据或内网 endpoint 写入受版本控制文件。
4. 检查 Model Profile certification enforcement。没有认证记录的旧 Profile 继续
   等价于 `contract_only` 且 enforcement 为 `off`，不会改变旧任务行为。

## 部署

安装准确 RC wheel 和所需 extra，然后显式 apply/verify migration：

```bash
python -m pip install 'ocrparser-platform[platform]==0.4.0rc2'
ocr-platform-migrate apply --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

PostgreSQL 未显式设置 `OCR_PLATFORM_AUTO_MIGRATE=1` 时，startup
auto-migration 默认关闭。生产环境应保持关闭并使用 migration CLI。SQLite 保留
本地开发便利行为。

启动 Control 后同时检查：

```bash
curl --fail http://127.0.0.1:8080/healthz
curl --fail http://127.0.0.1:8080/readyz
```

`/healthz` 表示进程健康。PostgreSQL schema drift 阻塞业务 API 时，`/readyz`
返回 `503`，并给出恢复 readiness 所需的 migration 命令。

Agent 可以通过 `--engine_provenance_file` 或
`OCR_AGENT_ENGINE_PROVENANCE_FILE` 提供脱敏 provenance 文件。Heartbeat 只上报
revision/digest；文件中不得包含 endpoint、凭据、本地路径或文档正文。

## 运维核对

- 使用现有 API token 查询 `/api/system/metrics`，并加载
  [告警示例](control-alerts.zh-CN.md)。
- 检查 diagnostics 的 `capacity`、`audit` 和 `alerts`。容量结果只提供建议，不会
  自动扩缩 Worker 或模型服务。
- 启用 `verified` 或 `certified` enforcement 前先验证认证 preflight。
- 确认 `/source.json` 显示版本 `0.4.0rc2`、准确 wheel revision、
  `build_dirty=false` 和 `release_build=true`。

## 回滚

v0.4 不新增 `0020` 之后的 migration。优先回滚到最新、已验证的 v0.3 维护
wheel。包含 `0020` 的数据库最老只支持回滚到 v0.3.2，且必须先完成 migration、
Worker 和认证兼容检查；不得使用 v0.3.1。回滚到 v0.3.2 以下必须恢复经过验证的
0020 之前快照。

回滚前停止新任务，根据恢复手册完成或停止运行中任务，部署目标 wheel，执行
migration `verify`，再恢复 Agent。checksum mismatch、未知 migration、replay
卡住或数据完整性失败都会阻塞回滚完成。
