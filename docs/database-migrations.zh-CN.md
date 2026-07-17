# 数据库迁移

OcrParser 在 `ocr_platform/control/migrations/` 中保留有序的 PostgreSQL SQL
历史。v0.3 新增统一 `MigrationRunner`，Control startup、Deployment Doctor、CI
和 migration CLI 全部使用同一实现；不引入 Alembic，也不重写 `0001` 至 `0018`。

安装 platform extra 并设置生产数据库地址：

```bash
export OCR_PLATFORM_DATABASE_URL='postgresql+psycopg://user:password@db/ocr_platform'
ocr-platform-migrate status
ocr-platform-migrate plan
ocr-platform-migrate apply
ocr-platform-migrate verify
```

`apply` 会取得 PostgreSQL transaction advisory lock，按文件名顺序应用待执行 SQL，
并记录 SHA-256 checksum。`0019` 增加 checksum 列，并为历史 migration 记录回填当前
package 中的 checksum。若已应用 SQL 的内容与记录不一致，`apply` 会拒绝继续执行。

v0.3 仍保留 Control startup 自动迁移，但生产部署推荐显式执行 `plan`、`apply` 和
`verify`，使错误在服务重启前暴露。生产升级前仍应备份数据库，并先在 staging 副本
上验证迁移。
