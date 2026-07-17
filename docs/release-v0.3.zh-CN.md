# v0.3 发布检查单

[English](release-v0.3.md)

1. 确认公开 `main` clean，Python 3.10-3.12 CI 全绿；
2. 在空环境验证 base/platform/s3/layout/full wheel；
3. 验证 PostgreSQL migration/checksum、并发 claim/lease/recovery、mock Control →
   Agent → Artifact、输出审计和性能门禁；
4. 确认全部 UI module 和 migration SQL 都进入 wheel package data；
5. 使用 `tools/verify_release_build.py` 核对 `_build_info.json`、`/source.json`、
   AGPL license/source route 和 tag commit；
6. 发布 `v0.3.0rc1`；真实模型认证使用该固定 commit，不在 GitHub release job 中
   启动 GPU；
7. 候选验收后更新 release notes，从 clean commit 以同样门禁发布 `v0.3.0`。
