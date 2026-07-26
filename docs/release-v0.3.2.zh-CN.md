# v0.3.2 数据库兼容桥接版本

[English](release-v0.3.2.md) | 中文

v0.3.2 是一个范围严格受限的数据库兼容版本，用于为 v0.4 的认证 engine
profile 做准备。它不启用认证策略，也不改变现有 CLI、HTTP/OpenAPI、Job
preflight、状态、manifest 或输出契约。

## 变化

- Migration `0020_model_profile_certification` 为每个 model profile 增加一条
  可选的一对一认证 provenance 记录。
- 不为已有 model profile 自动补行，它们的行为与 v0.3.1 完全相同。
- PostgreSQL 在 ORM 建表前执行带 checksum 校验的 migration，使 migration
  catalog 成为生产 schema 的权威来源。
- SQLite 保留直接本地开发所需的 create-all 行为。

新表在 v0.3.2 中保持休眠。HTTP 请求和响应不暴露其中的字段，也不会根据认证记录
接受或拒绝 Job。该表仅用于未来保存不可变 revision/digest 和可审计风险接受记录；
不得保存凭据、内网 endpoint、OCR 正文或客户文档。

## 升级

先备份数据库并部署准确的 v0.3.2 wheel，然后使用统一 migration runner：

```bash
ocr-platform-migrate plan --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate apply --database-url "$OCR_PLATFORM_DATABASE_URL"
ocr-platform-migrate verify --database-url "$OCR_PLATFORM_DATABASE_URL"
```

生产环境开始接收新任务前，应确认
`0020_model_profile_certification` 已经处于 current 状态。

## 回滚

应用 0020 之后的回滚下限是 v0.3.2。只要没有应用 0020 之后的 migration，
后续 v0.4 Control 可以回到 v0.3.2，并保留这张 additive 表，无需执行 schema
downgrade。

不要让 v0.3.1 直接连接包含 0020 的数据库。它的旧 migration catalog 会正确地把
0020 判定为 unexpected，并阻止生产 Job preflight。若必须回到 v0.3.1，需要恢复
经过验证的 0020 前数据库快照。

## 发布门禁

Tag 前需要通过 Python 和安装矩阵、SQLite/PostgreSQL migration、并发 apply、
package data、准确 wheel provenance、文档链接和 AGPL source offer 验证。
这个仅包含 schema 桥接的版本不需要启动 GPU 或 OCR 模型服务。
