from tools.check_performance_regression import aggregate_throughput, regression_percent


def test_performance_guard_aggregates_pages_over_wall_time() -> None:
    rows = [
        {"variant": "baseline", "status": "ok", "pages": "4", "duration_s": "2"},
        {"variant": "baseline", "status": "failed", "pages": "100", "duration_s": "1"},
        {"variant": "baseline", "status": "ok", "pages": "6", "duration_s": "3"},
    ]
    assert aggregate_throughput(rows, "baseline") == 2.0


def test_performance_guard_detects_more_than_ten_percent_regression() -> None:
    assert regression_percent(10.0, 9.0) == 10.0
    assert regression_percent(10.0, 8.9) > 10.0
    assert regression_percent(10.0, 11.0) == 0.0
