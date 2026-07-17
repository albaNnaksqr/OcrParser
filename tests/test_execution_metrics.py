from __future__ import annotations

from ocr_parser.infra import metrics


class _MetricRecorder:
    def __init__(self):
        self.labels_seen = []

    def labels(self, **labels):
        self.labels_seen.append(labels)
        return self

    def inc(self):
        return None


def test_execution_metrics_bound_unknown_labels(monkeypatch):
    stages = _MetricRecorder()
    fallbacks = _MetricRecorder()
    monkeypatch.setattr(metrics, "ENGINE_STAGE_OUTCOMES", stages)
    monkeypatch.setattr(metrics, "ENGINE_FALLBACKS", fallbacks)

    metrics.record_engine_execution_trace(
        "customer-engine-123",
        {
            "stages": [
                {
                    "stage": "request-id-456",
                    "status": "mystery",
                    "failure_category": "exception-with-user-data",
                }
            ],
            "fallback": {
                "used": True,
                "source_stage": "request-id-456",
                "reason": "raw exception text",
            },
        },
    )

    assert stages.labels_seen == [
        {
            "engine": "other",
            "stage": "other",
            "status": "other",
            "failure_category": "other",
        }
    ]
    assert fallbacks.labels_seen == [
        {"engine": "other", "source_stage": "other", "reason": "other"}
    ]
