# Control 告警示例

English mirror: [control-alerts.md](control-alerts.md)

Control 在受 token 保护的 `/api/system/metrics` 输出 Prometheus 指标；`/api/system/diagnostics` 同时提供固定的 alert code 和 recommendation code。以下规则仅作为运维起点，Control 不会自动扩缩容或修复基础设施。

## Prometheus 示例

```yaml
groups:
  - name: ocrparser-control
    rules:
      - alert: OcrParserShardQueueBlocked
        expr: sum(ocr_platform_shard_queue{status="pending"}) > 0 and sum(ocr_platform_worker_slots_by_status{status=~"idle|online"}) == 0
        for: 10m
        labels:
          severity: warning
        annotations:
          action: add_worker_capacity

      - alert: OcrParserStageFailures
        expr: sum(ocr_platform_stage_outcomes{status="failed"}) > 0
        for: 5m
        labels:
          severity: error
        annotations:
          action: investigate_stage_failures

      - alert: OcrParserFallbackUsage
        expr: sum(ocr_platform_fallbacks) > 0
        for: 15m
        labels:
          severity: warning
        annotations:
          action: review_fallback_usage
```

Migration drift、Worker 心跳过期、shard lease 过期、spool backlog、manifest 完整性、artifact 记录缺失和 output audit 证据缺失通过 `/api/system/diagnostics` 的固定 alert code 暴露。告警系统应路由对应的固定 `recommendation_code`，不要把自由文本错误写入 label。

## Label 安全

系统只输出有限集合的 `engine`、`stage`、`status`、`failure_category` 和 `fallback_category` 标签。不要把 Job ID、路径、endpoint、错误消息、凭据或文档内容加入 label 或 annotation。
