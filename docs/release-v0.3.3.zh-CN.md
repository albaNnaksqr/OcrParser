# v0.3.3 Lease Attempt Cap 维护版本

[English](release-v0.3.3.md) | 中文

v0.3.3 是一个聚焦 Control 调度与恢复的维护版本。它避免 lease 过期恢复超过
Job 配置的 shard attempt 上限，并加强并发终态处理。

本版本不改变 CLI、HTTP/OpenAPI 契约、数据库 schema 与 migration 历史、
manifest 与输出格式、状态词汇或 Parser 算法。

## 恢复修复

- Running shard 的 lease 在 `max_shard_attempts` 处过期时，shard 及其当前
  attempt 进入 terminal `failed`，并使用有界的 `lease_expired` 类别。Claim
  路径不再创建第 `N+1` 次 attempt。
- Job stop 请求优先于 lease exhaustion failure，因此相关 shard、attempt 和 Job
  记录会收敛到已有的 stopped 结果。
- Terminal shard update 保持单调且幂等。迟到 replay 不能回退已耗尽或已停止的
  shard；server 或 attempt 不匹配的 update 仍会被 fence。
- Job 终态归因与 shard 终态修改串行化，使并发 success、failure、stop、
  reclaim 和 replay 路径确定性收敛。

本版本增加 PostgreSQL 并发覆盖，检查同时 claim、lease exhaustion、stop、
worker re-registration、heartbeat 和竞争 terminal update。它不改变 claim
顺序、lease 时长、状态值或公开 API。

## 升级与回滚

按照常规 Control 维护流程部署准确的 v0.3.3 wheel。本版本没有新增 migration。
已经处于 `0020_model_profile_certification` 的部署仍保持 current：

```bash
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

已有 Agent 配置和命令行无需调整。升级后应观察 stale 与 failed shard，确认 attempt
耗尽的 lease 以 `lease_expired` 收敛，而不是再获得一次额外 attempt。

由于 v0.3.3 不改变 schema，Control 回滚到 v0.3.2 不需要执行数据库 downgrade。
Migration 0020 建立的 v0.3.2 回滚下限仍然有效。

## 发布门禁

创建 tag 前，应验证 Python 3.10-3.12 测试矩阵、定向 SQLite/PostgreSQL
恢复测试、安装 profile、文档链接、migration checksum 和 package data。从最终
clean commit 构建发布 wheel，并确认版本与 source revision 匹配 tag、
`dirty=false`，且 `/source.json` 报告 `release_build=true`。

发布这个维护版本不需要启动 OCR 或 GPU 模型服务。
