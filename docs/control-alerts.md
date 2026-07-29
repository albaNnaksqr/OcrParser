# Control Alert Examples

Chinese mirror: [control-alerts.zh-CN.md](control-alerts.zh-CN.md)

Control exposes a token-protected Prometheus snapshot at
`/api/system/metrics`. `/api/system/diagnostics` also returns stable alert codes
and recommendation codes. These examples are operator-owned starting points;
Control does not automatically scale or repair infrastructure.

## Prometheus Examples

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

Migration drift, stale worker heartbeat, stale shard leases, spool backlog,
manifest integrity, missing artifact records, and missing output-audit evidence
are exposed by `/api/system/diagnostics` as fixed alert codes. Route those codes
to the corresponding fixed `recommendation_code`; do not copy free-form error
text into alert labels.

## Label Safety

Only the bounded labels `engine`, `stage`, `status`, `failure_category`, and
`fallback_category` are emitted. Do not add job IDs, paths, endpoints, error
messages, credentials, or document content to labels or annotations.
