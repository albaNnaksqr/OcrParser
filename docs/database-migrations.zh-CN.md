# 数据库迁移

OcrParser 在 `ocr_platform/control/migrations/` 中保留有序的 PostgreSQL SQL
历史。Control startup 检查、Deployment Doctor、CI 和 migration CLI 统一使用
`MigrationRunner`；不引入 Alembic，也不重写历史 migration。

安装 platform extra 并设置生产数据库地址：

```bash
export OCR_PLATFORM_DATABASE_URL='postgresql+psycopg://user:password@db/ocr_platform'
export OCR_PLATFORM_AUTO_MIGRATE=0
ocr-platform-migrate status
ocr-platform-migrate plan
ocr-platform-migrate apply
ocr-platform-migrate verify
```

`apply` 会取得 PostgreSQL transaction advisory lock，按文件名顺序应用待执行 SQL，
并记录 SHA-256 checksum。`0019` 增加 checksum 列，并为历史 migration 记录回填当前
package 中的 checksum。若已应用 SQL 的内容与记录不一致，`apply` 会拒绝继续执行。

PostgreSQL 默认不在 startup 自动迁移。重启 Control 前应显式执行 `plan`、`apply`
和 `verify`，使错误在新进程接收流量前暴露。只有部署流程明确由 startup 管理迁移时，
才设置 `OCR_PLATFORM_AUTO_MIGRATE=1`。设置
`OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS=1` 可在 schema drift 时严格拒绝启动；
否则进程继续存活，`/readyz` 与业务 API 返回 `503`，diagnostics 仍可访问。

Control 运行期间从外部完成 migration 后，readiness 会恢复，但读取 ModelProfile
不会隐式创建默认记录。请在 `verify` 后重启 Control，或显式执行：

```bash
python -m ocr_platform.control.bootstrap
```

生产升级前应备份数据库，并先在 staging 副本上验证迁移。systemd unit 不会隐式
执行 migration。
