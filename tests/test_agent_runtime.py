from __future__ import annotations

import asyncio
import sys

from ocr_platform.agent.config import AgentConfig
from ocr_platform.agent.runtime import AgentRuntime, AgentSupervisor


class RuntimeClient:
    def __init__(self) -> None:
        self.heartbeats: list[dict[str, object]] = []
        self.events: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    async def heartbeat(self, **payload):
        self.heartbeats.append(payload)

    async def post_event(self, job_id, payload):
        self.events.append((job_id, payload))

    async def close(self):
        self.closed = True


def make_config(tmp_path) -> AgentConfig:
    return AgentConfig(
        server_id="server-a",
        control_url="http://control:8080",
        work_dir=str(tmp_path / "work"),
        python_executable=sys.executable,
        resource_guard_enabled=False,
        poll_interval_seconds=0.01,
        heartbeat_interval_seconds=0.01,
    )


def test_runtime_starts_six_named_lanes(tmp_path):
    async def exercise():
        runtime = AgentRuntime(make_config(tmp_path), client=RuntimeClient())
        runtime.start_lanes()
        assert tuple(runtime.supervisor.tasks) == AgentRuntime.LANE_NAMES
        await runtime.supervisor.shutdown()

    asyncio.run(exercise())


def test_supervisor_cancels_all_lane_tasks():
    cancelled: list[str] = []

    async def exercise():
        supervisor = AgentSupervisor()

        async def lane(name):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(name)

        supervisor.start_lane("one", lambda: lane("one"))
        supervisor.start_lane("two", lambda: lane("two"))
        await asyncio.sleep(0)
        await supervisor.shutdown()
        assert all(task.done() for task in supervisor.tasks.values())

    asyncio.run(exercise())
    assert sorted(cancelled) == ["one", "two"]


def test_spool_replay_does_not_heartbeat_after_shutdown(tmp_path):
    async def exercise():
        client = RuntimeClient()
        runtime = AgentRuntime(make_config(tmp_path), client=client)

        async def stop_during_replay():
            runtime.request_shutdown()
            return 1

        runtime._replay_once = stop_during_replay
        await runtime._spool_replay_lane()
        assert client.heartbeats == []

    asyncio.run(exercise())


def test_shard_lane_does_not_report_failure_after_shutdown(tmp_path, monkeypatch):
    async def exercise():
        client = RuntimeClient()
        runtime = AgentRuntime(make_config(tmp_path), client=client)

        async def stop_then_fail(job, config, control_client):
            runtime.request_shutdown()
            raise RuntimeError("late failure")

        monkeypatch.setattr("ocr_platform.agent.runtime.run_job", stop_then_fail)
        await runtime.job_queue.put({"id": "job-1"})
        await runtime._shard_execution_lane()
        assert client.events == []
        assert runtime.current_job_id is None

    asyncio.run(exercise())
