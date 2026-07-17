# RFC：OcrParser v0.4 运维成熟度

[English](rfc-v0.4.md) | 中文

状态：等待 v0.3.1 稳定化完成后评审。本 RFC 不在 v0.3.1 维护版本中实施。

## 决策摘要

v0.4 优先实现显式运维、认证 engine profile、告警、容量规划和审计能力，不为拆分而
继续拆分，也不重写 OCR、layout、table 或 Markdown 算法。

## Migration 策略

- 新增 `OCR_PLATFORM_AUTO_MIGRATE`。生产 PostgreSQL 默认关闭自动 migration，必须
  在 Control 启动前执行 `ocr-platform-migrate plan|apply|verify`。
- 开发 helper 显式开启自动 migration。只有未启用生产保护的直接 SQLite 开发启动
  才能保留便利行为。
- schema 不一致时 Control readiness 返回可操作的 migration 指令；生产默认关闭时
  禁止静默升级。
- 保留现有 migration history/checksum，不采用 Alembic。

## 兼容策略

- v0.4 删除 `ocr_platform.control.service` 兼容 façade；集成方必须在升级前迁移到对应
  Control domain。
- v0.4 全周期继续接受和输出 `success_fallback_text`、
  `success_fallback_image`。新消费者必须使用结构化 `stages/fallback`；最早 v0.5
  才能在独立兼容决策后考虑移除。
- 除非后续 RFC 明确修改，否则保持 console scripts、HTTP paths、Job/Shard state、
  manifest JSONL、输出格式和 Parser 顶层 façade。

## 认证 Engine Profile

- Profile 绑定 parser revision、model revision、runtime image digest/source revision、
  可选 layout revision、fixture-set digest 和认证状态。
- Profile 要求认证时，job preflight 对缺失或变化的 provenance 字段直接拒绝。对于
  `Verified` profile，只允许显式、可审计的风险接受模式。
- Profile secret 继续只引用环境变量；认证 metadata 不保存 key 或内网 endpoint
  credential。

## 可观测性与容量

- 提供固定 label 的 stage failure rate、fallback rate、worker heartbeat age、stale
  lease、spool backlog、migration drift 和 artifact audit failure 告警示例。
- 增加只读容量输出，综合 worker slot、queue depth、近期 page duration、API
  concurrency 和资源预算；仅提供建议，不自动扩缩 worker 或模型服务。
- 保留足够的 job/shard/attempt 审计证据，用于解释 reclaim、replay、stop/resume 和
  output provenance，但不记录文档正文。

## 进入条件

只有 v0.3.1 完成 Spark soak，且随后至少观察七天未出现新的 P0/P1 稳定性缺陷，才
开始实施。任何未关闭的数据丢失、重复 claim、migration、replay、shutdown 或资源
泄漏问题继续作为 v0.3 维护优先项。
