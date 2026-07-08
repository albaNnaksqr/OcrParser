from ocr_platform.agent.resources import collect_system_resources, evaluate_resource_pressure


def test_collect_system_resources_reports_host_and_disk_metrics(tmp_path):
    resources = collect_system_resources(paths=[str(tmp_path)])

    assert resources["checked_at"]
    assert resources["cpu"]["logical_count"] >= 1
    assert "load_avg_1m" in resources["cpu"]
    assert resources["memory"]["total_bytes"] >= 0
    assert resources["memory"]["percent"] >= 0

    disk = resources["disks"][0]
    assert disk["path"] == str(tmp_path)
    assert disk["exists"] is True
    assert disk["total_bytes"] >= disk["free_bytes"] >= 0
    assert disk["percent"] >= 0


def test_evaluate_resource_pressure_blocks_on_memory_or_disk_pressure():
    resources = {
        "memory": {
            "percent": 91,
            "available_bytes": 8 * 1024**3,
        },
        "disks": [
            {
                "path": "/shared",
                "percent": 50,
                "free_bytes": 20 * 1024**3,
            },
            {
                "path": "/output",
                "percent": 96,
                "free_bytes": 20 * 1024**3,
            },
        ],
    }

    pressure = evaluate_resource_pressure(
        resources,
        memory_percent_threshold=90,
        min_available_memory_bytes=4 * 1024**3,
        disk_percent_threshold=95,
        min_free_disk_bytes=10 * 1024**3,
    )

    assert pressure["constrained"] is True
    assert pressure["level"] == "blocked"
    assert any("memory percent" in reason for reason in pressure["reasons"])
    assert any("/output disk percent" in reason for reason in pressure["reasons"])


def test_evaluate_resource_pressure_allows_healthy_resources():
    resources = {
        "memory": {
            "percent": 50,
            "available_bytes": 16 * 1024**3,
        },
        "disks": [
            {
                "path": "/shared",
                "percent": 70,
                "free_bytes": 100 * 1024**3,
            },
        ],
    }

    pressure = evaluate_resource_pressure(resources)

    assert pressure["constrained"] is False
    assert pressure["level"] == "ready"
    assert pressure["reasons"] == []
