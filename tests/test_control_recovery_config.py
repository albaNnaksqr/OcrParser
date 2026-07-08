import json
import os
import subprocess
import sys


def load_timeout_values(extra_env=None):
    env = os.environ.copy()
    for key in (
        "OCR_JOB_STALE_AFTER_SECONDS",
        "OCR_SERVER_STALE_AFTER_SECONDS",
        "OCR_SHARD_LEASE_SECONDS",
    ):
        env.pop(key, None)
    if extra_env:
        env.update(extra_env)

    output = subprocess.check_output(
        [
            sys.executable,
            "-c",
            (
                "import json\n"
                "from ocr_platform.control import service\n"
                "print(json.dumps({\n"
                "  'job_stale': service.STALE_AFTER_SECONDS,\n"
                "  'server_stale': service.SERVER_STALE_AFTER_SECONDS,\n"
                "  'shard_lease': service.SHARD_LEASE_SECONDS,\n"
                "}))\n"
            ),
        ],
        env=env,
        text=True,
    )
    return json.loads(output)


def test_recovery_timeouts_keep_default_values():
    assert load_timeout_values() == {
        "job_stale": 120,
        "server_stale": 120,
        "shard_lease": 180,
    }


def test_recovery_timeouts_can_be_configured_from_environment():
    assert load_timeout_values(
        {
            "OCR_JOB_STALE_AFTER_SECONDS": "45",
            "OCR_SERVER_STALE_AFTER_SECONDS": "60",
            "OCR_SHARD_LEASE_SECONDS": "30",
        }
    ) == {
        "job_stale": 45,
        "server_stale": 60,
        "shard_lease": 30,
    }


def test_invalid_recovery_timeout_environment_uses_defaults():
    assert load_timeout_values(
        {
            "OCR_JOB_STALE_AFTER_SECONDS": "0",
            "OCR_SERVER_STALE_AFTER_SECONDS": "-1",
            "OCR_SHARD_LEASE_SECONDS": "not-an-int",
        }
    ) == {
        "job_stale": 120,
        "server_stale": 120,
        "shard_lease": 180,
    }
