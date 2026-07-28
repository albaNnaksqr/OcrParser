# v0.3.4 PostgreSQL Claim 稳定性维护版本

[English](release-v0.3.4.md) | 中文

v0.3.4 是一个聚焦 Control 调度稳定性的维护版本。它消除了 PostgreSQL 并发
shard claim 与 shard 终态更新之间的锁顺序反转。

本版本不改变 CLI、HTTP/OpenAPI 契约、数据库 schema 与 migration 历史、
manifest 与输出格式、状态词汇、claim 顺序或 Parser 算法。

## 稳定性修复

- Shard claim 在选择并锁定 WorkShard 前，先对所属 Job 获取共享锁。
- 并发 claimer 之间仍可兼容执行，WorkShard 选择继续使用现有
  `FOR UPDATE SKIP LOCKED` 行为。
- Terminal update 保持现有 Job 到 WorkShard 的锁顺序，从而消除 PostgreSQL
  高并发下可能发生死锁的反向锁依赖。
- 现有 eligibility 检查、attempt fencing、lease recovery、终态单调性和
  replay 行为保持不变。

维护候选通过了确定性并发碰撞覆盖、多 worker PostgreSQL claim 压力验证、
常规测试矩阵和包验证。这些门禁只验证 Control 稳定性，不要求启动 OCR 或 GPU
模型服务。

## 升级与回滚

按照常规 Control 维护流程部署准确的 v0.3.4 wheel。本版本没有新增 migration。
已经处于 `0020_model_profile_certification` 的部署仍保持 current：

```bash
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

已有 Agent 配置和命令行无需调整。由于 v0.3.4 不改变 schema，回滚到 v0.3.3
不需要执行数据库 downgrade。Migration 0020 建立的 v0.3.2 回滚下限仍然适用。

## 发布完整性

发布 wheel 必须从最终 clean 的 `v0.3.4` commit 构建。其内嵌 source revision
必须与 tag 一致，`dirty=false`，并且 `/source.json` 必须报告
`release_build=true` 及对应的公开源码地址。
