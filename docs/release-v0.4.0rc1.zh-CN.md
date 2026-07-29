# v0.4.0rc1 候选版本

[English](release-v0.4.0rc1.md) | 中文

`0.4.0rc1` 是 v0.4 Control 运维成熟度版本线的候选版本。Control 继续保持模块化
单体；本候选完成既定运维、所有权和兼容工作，不修改 Parser 算法或调度算法。

## 重点

- PostgreSQL startup migration 改为 opt-in；health/readiness 分离，schema drift
  会阻塞业务 API 并提供可执行诊断。
- Model Profile 可以记录认证和不可变 provenance。Enforcement 仍为 opt-in，旧
  Profile 默认不阻塞。
- Prometheus metrics 使用有界 label；diagnostics 提供 advisory capacity、audit
  和 alerts，不自动扩缩容。
- Application command 持有事务，query 保持只读；scheduling 拥有 claim、lease、
  attempt、fencing、replay 和 recovery transition。
- Jobs、manifests、workers 具有明确 command/query/policy owner。
- 所有仓库 consumer 迁移后，旧 `ocr_platform.control.service` façade 已删除。

删除该 façade 是 v0.4 唯一有意的 Python import breaking change。CLI、
HTTP/OpenAPI、数据库 schema/migration、状态值、manifest/output format 和
Parser 顶层兼容导入保持不变。

## 候选升级

请使用 [v0.4 升级指南](migration-v0.4.zh-CN.md)和
[façade symbol 迁移表](control-facade-migration.zh-CN.md)。PostgreSQL 生产部署
应关闭 startup auto-migration，并使用
`ocr-platform-migrate plan|apply|verify`。

Schema 上限仍为 migration `0020`，本候选不新增 `0021`。回滚优先选择最新、已
验证的 v0.3 维护 wheel；应用 `0020` 后最老兼容下限为 v0.3.2。

## 运维

检查当前 [Control 模块地图](control-module-map.zh-CN.md)和
[告警示例](control-alerts.zh-CN.md)。Metrics 和 diagnostics 继续由现有 API
token 保护。指标 label 或公开证据不得包含 job ID、文件路径、任意错误文本、
凭据、endpoint 或文档正文。

## RC 完整性

候选 wheel 必须从用于验证的 clean commit 构建。内嵌 `source_revision` 必须是
tag `v0.4.0rc1` 所指向的 commit SHA，而不是 tag 名称本身。安装后的
distribution version 必须为 `0.4.0rc1`；wheel provenance 和 `/source.json`
必须报告该 commit，并且 `dirty=false`、`release_build=true`。

在 release commit 和 tag 生成之前构建的 wheel 只能作为 package preflight
产物。创建 release commit 并让 `v0.4.0rc1` 指向该 commit 后，必须从这个准确的
clean commit 重新构建 wheel，再次验证 provenance 和安装门禁，才能发布。

创建 RC tag 前，自动 package、migration、recovery、安全、contract 和安装门禁
必须通过。随后使用准确候选执行隔离恢复和引擎集成门禁；这些门禁不与历史模型质量
或吞吐作比较。构建或发布候选不需要启动 GPU 模型服务。

本文描述候选版本，不代表最终 `v0.4.0`。只有准确 RC 验收通过且没有 tracked
runtime 改动后，才能进入最终发布。
