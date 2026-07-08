import asyncio
import signal
import sys

from ocr_platform.agent.config import AgentConfig
from ocr_platform.agent import runner
from ocr_platform.agent.runner import should_stop_job


class FakeClient:
    def __init__(self, stop_requested):
        self.stop_requested = stop_requested

    async def get_job(self, job_id):
        return {"id": job_id, "stop_requested": self.stop_requested}


def test_should_stop_job_reads_control_flag():
    assert asyncio.run(should_stop_job("job-1", FakeClient(True))) is True
    assert asyncio.run(should_stop_job("job-1", FakeClient(False))) is False


class FakeStatusClient:
    async def get_job(self, job_id):
        return {"id": job_id, "status": "stopping"}


def test_should_stop_job_treats_stopping_status_as_stop():
    assert asyncio.run(should_stop_job("job-1", FakeStatusClient())) is True


class StopRunClient:
    def __init__(self):
        self.events = []

    async def get_job(self, job_id):
        return {"id": job_id, "stop_requested": True}

    async def post_event(self, job_id, event):
        self.events.append(event)

    async def post_log(self, job_id, stream, line):
        pass


class LaunchFailureClient:
    def __init__(self):
        self.events = []

    async def post_event(self, job_id, event):
        self.events.append((job_id, event))


def test_run_job_posts_failed_when_command_build_raises(tmp_path, monkeypatch):
    def fake_build_ocr_command(job, config):
        raise RuntimeError("cannot build command")

    monkeypatch.setattr(runner, "build_ocr_command", fake_build_ocr_command)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = LaunchFailureClient()

    return_code = asyncio.run(runner.run_job({"id": "job-1"}, config, client))

    assert return_code == 1
    assert client.events == [
        (
            "job-1",
            {
                "type": "job_failed",
                "payload": {
                    "failure_category": "runner_start_failed",
                    "error_message": "cannot build command",
                },
            },
        )
    ]


def test_run_job_posts_failed_when_subprocess_launch_raises(tmp_path, monkeypatch):
    event_file = tmp_path / "events.jsonl"

    def fake_build_ocr_command(job, config):
        return [sys.executable, "-c", "raise SystemExit(0)"], event_file

    async def fake_create_subprocess_exec(*args, **kwargs):
        raise OSError("spawn failed")

    monkeypatch.setattr(runner, "build_ocr_command", fake_build_ocr_command)
    monkeypatch.setattr(
        runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = LaunchFailureClient()

    return_code = asyncio.run(runner.run_job({"id": "job-1"}, config, client))

    assert return_code == 1
    assert client.events == [
        (
            "job-1",
            {
                "type": "job_failed",
                "payload": {
                    "failure_category": "runner_start_failed",
                    "error_message": "spawn failed",
                },
            },
        )
    ]


def test_run_job_posts_stopped_instead_of_failed_for_stop_request(tmp_path, monkeypatch):
    event_file = tmp_path / "events.jsonl"

    def fake_build_ocr_command(job, config):
        command = [
            sys.executable,
            "-c",
            "import time; time.sleep(30)",
        ]
        return command, event_file

    monkeypatch.setattr(runner, "build_ocr_command", fake_build_ocr_command)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StopRunClient()

    return_code = asyncio.run(runner.run_job({"id": "job-1"}, config, client))

    event_types = [event["type"] for event in client.events]
    assert return_code != 0
    assert "job_stopping" in event_types
    assert "job_stopped" in event_types
    assert "job_failed" not in event_types


def test_run_job_posts_stopped_for_zero_exit_when_stop_requested_after_wait(
    tmp_path, monkeypatch
):
    event_file = tmp_path / "events.jsonl"

    def fake_build_ocr_command(job, config):
        command = [
            sys.executable,
            "-c",
            "raise SystemExit(0)",
        ]
        return command, event_file

    async def fake_stop_watcher(job_id, client, process, **_kwargs):
        return False

    monkeypatch.setattr(runner, "build_ocr_command", fake_build_ocr_command)
    monkeypatch.setattr(runner, "_stop_watcher", fake_stop_watcher)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StopRunClient()

    return_code = asyncio.run(runner.run_job({"id": "job-1"}, config, client))

    event_types = [event["type"] for event in client.events]
    assert return_code == 0
    assert "job_stopped" in event_types
    assert "job_failed" not in event_types


def test_run_job_kills_child_that_ignores_sigterm_on_stop_request(
    tmp_path, monkeypatch
):
    event_file = tmp_path / "events.jsonl"

    def fake_build_ocr_command(job, config):
        command = [
            sys.executable,
            "-c",
            (
                "import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "time.sleep(30)\n"
            ),
        ]
        return command, event_file

    monkeypatch.setattr(runner, "build_ocr_command", fake_build_ocr_command)
    monkeypatch.setattr(runner, "TERMINATION_TIMEOUT_SECONDS", 0.05)
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
        process_termination_timeout_seconds=0.05,
        stop_poll_interval_seconds=0.01,
    )
    client = StopRunClient()

    return_code = asyncio.run(
        asyncio.wait_for(runner.run_job({"id": "job-1"}, config, client), timeout=3.0)
    )

    event_types = [event["type"] for event in client.events]
    assert return_code != 0
    assert event_types.index("job_stopping") < event_types.index("job_stopped")
    assert "job_failed" not in event_types


def test_subprocess_is_started_in_its_own_process_group(tmp_path, monkeypatch):
    event_file = tmp_path / "events.jsonl"
    captured_kwargs = {}

    def fake_build_ocr_command(job, config):
        return [sys.executable, "-c", "raise SystemExit(0)"], event_file

    class FakeProcess:
        pid = 12345
        stdout = None
        stderr = None
        returncode = 0

        async def wait(self):
            return 0

    async def fake_create_subprocess_exec(*args, **kwargs):
        captured_kwargs.update(kwargs)
        return FakeProcess()

    monkeypatch.setattr(runner, "build_ocr_command", fake_build_ocr_command)
    monkeypatch.setattr(
        runner.asyncio, "create_subprocess_exec", fake_create_subprocess_exec
    )
    config = AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path),
        python_executable=sys.executable,
    )
    client = StopRunClient()

    return_code = asyncio.run(runner.run_job({"id": "job-1"}, config, client))

    assert return_code == 0
    assert captured_kwargs["start_new_session"] is True


def test_terminate_process_signals_whole_process_group(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 12345
        returncode = None

        def send_signal(self, sig):
            calls.append(("send_signal", sig))

        def kill(self):
            calls.append(("kill", None))

        async def wait(self):
            self.returncode = -signal.SIGTERM
            return self.returncode

    def fake_killpg(pid, sig):
        calls.append(("killpg", pid, sig))

    monkeypatch.setattr(runner.os, "killpg", fake_killpg)

    asyncio.run(runner._terminate_process(FakeProcess()))

    assert ("killpg", 12345, signal.SIGTERM) in calls
    assert not any(call[0] == "send_signal" for call in calls)
