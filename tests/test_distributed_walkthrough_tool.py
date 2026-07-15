from argparse import Namespace

from tools import run_distributed_walkthrough


def test_walkthrough_tool_builds_distributed_mock_ocr_job_payload(tmp_path):
    args = Namespace(
        shared_root=str(tmp_path / "shared"),
        worker_id="local-worker-01",
        engine="dotsocr",
        ocr_host="127.0.0.1",
        ocr_port=18000,
        model_name="mock-ocr",
    )

    payload = run_distributed_walkthrough.build_job_payload(args)

    assert payload["input_mode"] == "distributed_remote_folder_snapshot"
    assert payload["input_dir"] == str(tmp_path / "shared" / "input")
    assert payload["output_dir"] == str(tmp_path / "shared" / "output")
    assert payload["manifest_root"] == str(tmp_path / "shared" / "manifests")
    assert payload["allowed_server_ids"] == ["local-worker-01"]
    assert payload["engine"] == "dotsocr"
    assert payload["ip"] == "127.0.0.1"
    assert payload["port"] == 18000
    assert payload["model_name"] == "mock-ocr"
    assert payload["extra_args"]["api_concurrency_max"] == 1
    assert payload["extra_args"]["no_warmup"] is True


def test_walkthrough_tool_compacts_job_summary_for_operator_logs():
    compact = run_distributed_walkthrough.compact_summary(
        {
            "status": "running",
            "lifecycle_stage": "running",
            "scan_status": "done",
            "completed_files": 0,
            "failed_files": 0,
            "total_files": 1,
            "pending_shards": 0,
            "running_shards": 1,
            "succeeded_shards": 0,
            "failed_shards": 0,
            "total_shards": 1,
            "pending_scan_units": 0,
            "running_scan_units": 0,
            "succeeded_scan_units": 1,
            "failed_scan_units": 0,
            "total_scan_units": 1,
            "worker_shards": [{"server_id": "local-worker-01"}],
            "attention_shards": [{"shard_index": 1}],
            "last_event_at": "2026-06-10T00:00:00Z",
        },
        3,
    )

    assert compact["poll"] == 3
    assert compact["files"] == [0, 0, 1]
    assert compact["shards"] == [0, 1, 0, 0, 1]
    assert compact["scan_units"] == [0, 0, 1, 0, 1]
    assert compact["worker_shards"] == [{"server_id": "local-worker-01"}]


def test_walkthrough_tool_can_reference_runtime_api_key_env_var(tmp_path):
    args = Namespace(
        shared_root=str(tmp_path / "shared"),
        worker_id="local-worker-01",
        engine="dotsocr",
        ocr_host="127.0.0.1",
        ocr_port=18000,
        model_name="mock-ocr",
        api_key_env_var="OCR_JOB_DOTSOCR_API_KEY",
    )

    payload = run_distributed_walkthrough.build_job_payload(args)

    assert payload["extra_args"]["api_key_env_var"] == "OCR_JOB_DOTSOCR_API_KEY"
