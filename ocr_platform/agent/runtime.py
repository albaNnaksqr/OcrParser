"""Composable agent runtime and lane supervisor."""

from __future__ import annotations

import asyncio
import logging
import signal
import socket
from contextlib import suppress
from typing import Awaitable, Callable

from .client import ControlClient
from .config import AgentConfig
from .lanes import (
    _heartbeat_capabilities,
    _job_exception_failure_payload,
    _next_job_if_resources_allow,
    _run_manifest_integrity_once,
    _run_scan_once,
)
from .runner import replay_pending_shard_updates, run_job


logger = logging.getLogger(__name__)
Lane = Callable[[], Awaitable[None]]


class AgentSupervisor:
    """Own all long-running lane tasks and coordinate one shutdown boundary."""

    def __init__(self) -> None:
        self.stop_event = asyncio.Event()
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self.failure: BaseException | None = None
        self.shutdown_signal: signal.Signals | None = None
        self._shutting_down = False

    @property
    def stopping(self) -> bool:
        return self.stop_event.is_set()

    def start_lane(self, name: str, lane: Lane) -> asyncio.Task[None]:
        if self.stopping:
            raise RuntimeError("cannot start an agent lane after shutdown begins")
        if name in self.tasks:
            raise ValueError(f"agent lane already started: {name}")
        task = asyncio.create_task(lane(), name=f"ocr-agent:{name}")
        self.tasks[name] = task
        task.add_done_callback(self._lane_done)
        return task

    def _lane_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled() or self._shutting_down:
            return
        try:
            error = task.exception()
        except asyncio.CancelledError:
            return
        if error is not None and self.failure is None:
            self.failure = error
            logger.error(
                "Agent lane failed",
                exc_info=(type(error), error, error.__traceback__),
            )
        self.stop_event.set()

    def request_shutdown(self, shutdown_signal: signal.Signals | None = None) -> None:
        if shutdown_signal is not None:
            self.shutdown_signal = shutdown_signal
        self.stop_event.set()

    def install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for signal_name in (signal.SIGINT, signal.SIGTERM):
            with suppress(NotImplementedError, RuntimeError, ValueError):
                loop.add_signal_handler(
                    signal_name,
                    self.request_shutdown,
                    signal_name,
                )

    async def wait(self) -> None:
        await self.stop_event.wait()

    async def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.stop_event.set()
        current = asyncio.current_task()
        pending = [task for task in self.tasks.values() if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


class AgentRuntime:
    """Single-process agent composed from six independently supervised lanes."""

    LANE_NAMES = (
        "heartbeat",
        "job-polling",
        "scan",
        "shard-execution",
        "manifest-integrity",
        "spool-replay",
    )

    def __init__(
        self,
        config: AgentConfig,
        client: ControlClient | None = None,
        supervisor: AgentSupervisor | None = None,
    ) -> None:
        self.config = config
        self.client = client or ControlClient(
            config.control_url,
            config.server_id,
            api_token=config.control_api_token,
            event_spool_dir=config.event_spool_dir,
            event_spool_max_bytes=config.event_spool_max_bytes,
        )
        self.supervisor = supervisor or AgentSupervisor()
        self.current_job_id: str | None = None
        self.job_queue: asyncio.Queue[dict[str, object]] = asyncio.Queue(maxsize=1)

    @property
    def stopping(self) -> bool:
        return self.supervisor.stopping

    async def _sleep_or_stop(self, delay: float) -> None:
        try:
            await asyncio.wait_for(self.supervisor.stop_event.wait(), timeout=max(delay, 0))
        except asyncio.TimeoutError:
            pass

    async def _heartbeat_lane(self) -> None:
        while not self.stopping:
            try:
                await self.client.heartbeat(
                    status="busy" if self.current_job_id else "idle",
                    current_job_id=self.current_job_id,
                    capabilities=_heartbeat_capabilities(self.config),
                )
            except Exception as exc:
                logger.warning("Heartbeat failed: %s", exc)
            await self._sleep_or_stop(self.config.heartbeat_interval_seconds)

    async def _job_polling_lane(self) -> None:
        while not self.stopping:
            if self.current_job_id is not None or not self.job_queue.empty():
                await self._sleep_or_stop(self.config.poll_interval_seconds)
                continue
            job = await _next_job_if_resources_allow(self.client, self.config)
            if job is None:
                await self._sleep_or_stop(self.config.poll_interval_seconds)
                continue
            await self.job_queue.put(job)

    async def _scan_lane(self) -> None:
        while not self.stopping:
            did_work = await _run_scan_once(self.client, self.config)
            await self._sleep_or_stop(0 if did_work else self.config.poll_interval_seconds)

    async def _shard_execution_lane(self) -> None:
        while not self.stopping:
            job = await self.job_queue.get()
            job_id = str(job["id"])
            self.current_job_id = job_id
            try:
                await run_job(job, self.config, self.client)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                if not self.stopping:
                    with suppress(Exception):
                        await self.client.post_event(
                            job_id,
                            {
                                "type": "job_failed",
                                "payload": _job_exception_failure_payload(exc),
                            },
                        )
            finally:
                self.current_job_id = None
                self.job_queue.task_done()

    async def _manifest_integrity_lane(self) -> None:
        while not self.stopping:
            did_work = await _run_manifest_integrity_once(self.client, self.config)
            await self._sleep_or_stop(0 if did_work else self.config.poll_interval_seconds)

    async def _replay_once(self) -> int:
        replayed = 0
        replay_events = getattr(self.client, "replay_spooled_events", None)
        if callable(replay_events):
            replayed += int(await replay_events() or 0)
        replay_logs = getattr(self.client, "replay_spooled_logs", None)
        if callable(replay_logs):
            replayed += int(await replay_logs() or 0)
        replayed += int(await replay_pending_shard_updates(self.config, self.client) or 0)
        return replayed

    async def _spool_replay_lane(self) -> None:
        while not self.stopping:
            try:
                replayed = await self._replay_once()
                if replayed and not self.stopping:
                    await self.client.heartbeat(
                        status="busy" if self.current_job_id else "idle",
                        current_job_id=self.current_job_id,
                        capabilities=_heartbeat_capabilities(self.config),
                    )
            except Exception as exc:
                logger.warning("Spool replay failed: %s", exc)
            await self._sleep_or_stop(self.config.poll_interval_seconds)

    def start_lanes(self) -> None:
        lanes = {
            "heartbeat": self._heartbeat_lane,
            "job-polling": self._job_polling_lane,
            "scan": self._scan_lane,
            "shard-execution": self._shard_execution_lane,
            "manifest-integrity": self._manifest_integrity_lane,
            "spool-replay": self._spool_replay_lane,
        }
        for name in self.LANE_NAMES:
            self.supervisor.start_lane(name, lanes[name])

    async def run(self) -> None:
        self.supervisor.install_signal_handlers()
        try:
            await self.client.register(host=socket.gethostname())
            self.start_lanes()
            await self.supervisor.wait()
            if self.supervisor.failure is not None:
                raise self.supervisor.failure
        finally:
            await self.supervisor.shutdown()
            await self.client.close()
        if self.supervisor.shutdown_signal is not None:
            raise SystemExit(128 + int(self.supervisor.shutdown_signal))

    def request_shutdown(self) -> None:
        self.supervisor.request_shutdown()
