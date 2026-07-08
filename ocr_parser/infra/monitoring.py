from __future__ import annotations

import time
from collections import defaultdict

from .metrics import INFERENCE_DURATION, INFERENCE_REQUESTS, RETRIES


class PerformanceMonitor:
    def __init__(self):
        self.stats = defaultdict(list)
        self.start_time = time.time()

    def record_inference_time(self, time_taken):
        self.stats["inference_times"].append(time_taken)
        INFERENCE_DURATION.observe(time_taken)
        INFERENCE_REQUESTS.labels(status="success").inc()

    def record_error(self, error_type):
        self.stats["errors"].append(error_type)
        INFERENCE_REQUESTS.labels(status="error").inc()

    def record_retry(self, attempt_count):
        self.stats["retry_attempts"].append(attempt_count)
        RETRIES.inc(attempt_count)

    def get_summary(self):
        total_time = time.time() - self.start_time
        inference_times = self.stats["inference_times"]
        retry_attempts = self.stats.get("retry_attempts", [])
        summary = {
            "total_time": total_time,
            "total_requests": len(inference_times),
            "avg_inference_time": sum(inference_times) / len(inference_times) if inference_times else 0,
            "total_errors": len(self.stats["errors"]),
            "total_retries": sum(retry_attempts) if retry_attempts else 0,
            "error_types": defaultdict(int),
        }
        for error in self.stats["errors"]:
            summary["error_types"][error] += 1
        return summary
