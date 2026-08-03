# v0.4.0 正式版本

[English](release-v0.4.0.md) | 中文

`0.4.0` 将已验收的 v0.4 候选版本提升为正式版本。Control 继续保持模块化单体，
同时明确 migration readiness、调度所有权、运维可见性和引擎 provenance。

## 主要变化

- PostgreSQL startup migration 改为显式启用，区分进程健康与 schema readiness，
  并统一使用带 checksum 的 migration runner。
- Model Profile 支持可选认证元数据，Agent provenance 可以参与 preflight，且不
  上报 endpoint 或凭据。
- Prometheus metrics、容量建议、审计摘要和告警模板使用有界 label，并继续由
  运维人员控制。
- Control 的事务、调度、任务、manifest 和 worker 所有权已经明确，同时保持
  HTTP/OpenAPI、数据库、状态、manifest、输出和调度行为。
- Agent event/log replay 按 spool 串行，并基于最新磁盘状态确认记录，避免丢失
  replay 期间追加的内容。

## 兼容性

唯一有意的 Python import breaking change 是删除
`ocr_platform.control.service`。Python 集成应改用文档中的 domain command、query
和 schema。现有 console script、CLI 参数、HTTP 路径和字段、截至 `0020` 的
migration 历史、状态值、manifest/output 格式及 Parser 顶层兼容导入继续保持。

## 验证

候选版本已通过 Python 3.10–3.12、PostgreSQL、安装矩阵、mock E2E、恢复、来源
和观察期门禁。DotsOCR、MinerU、PaddleOCR-VL 使用公开 fixture 完成复验；既有
质量和 runtime 限制保持不变，认证状态不自动升级。

安装所需 profile，并按照 [v0.4 升级指南](migration-v0.4.zh-CN.md)操作：

```bash
python -m pip install 'ocrparser-platform[platform]==0.4.0'
```

发布 wheel 和 `/source.json` 必须显示准确的 `v0.4.0` source revision、
`dirty=false` 和 `release_build=true`。
