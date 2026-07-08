import asyncio

from ocr_parser import runtime


class DummyParser:
    def __init__(self):
        self.api_limiter = runtime.ResizableAsyncLimiter(8)
        self.api_concurrency = 8
        self.api_concurrency_start = 8
        self.api_concurrency_max = 8


def test_apply_execution_control_payload_resizes_and_pauses_api_limiter():
    async def exercise():
        parser = DummyParser()

        result = await runtime.apply_execution_control_payload(
            parser,
            {
                "paused": True,
                "api_concurrency_limit": 2,
                "reason": "memory_pressure",
            },
        )

        assert result == {
            "changed": True,
            "paused": True,
            "api_concurrency_limit": 2,
            "reason": "memory_pressure",
        }
        assert parser.api_limiter.limit == 2
        assert parser._execution_control_paused is True
        assert parser._execution_resume_event.is_set() is False

    asyncio.run(exercise())


def test_api_lane_waits_while_execution_control_is_paused():
    async def exercise():
        parser = DummyParser()
        await runtime.apply_execution_control_payload(parser, {"paused": True})
        entered = False

        async def enter_lane():
            nonlocal entered
            async with runtime.api_lane(parser):
                entered = True

        task = asyncio.create_task(enter_lane())
        await asyncio.sleep(0)
        assert entered is False

        await runtime.apply_execution_control_payload(parser, {"paused": False})
        await asyncio.wait_for(task, timeout=1)
        assert entered is True

    asyncio.run(exercise())


def test_api_autotune_does_not_raise_limit_while_execution_control_is_paused():
    async def exercise():
        parser = DummyParser()
        parser.enable_api_autotune = True
        parser.api_concurrency_max = 16
        parser.api_autotune_last_error_count = 0
        parser.api_autotune_last_timeout_count = 0
        parser._api_inflight = 1
        parser._api_inflight_peak = 1
        parser._api_waiting = 4
        parser._api_call_count = 20
        parser._api_wait_seconds_total = 1.0
        parser._api_error_count = 0
        parser._api_timeout_count = 0

        await runtime.apply_execution_control_payload(
            parser,
            {
                "paused": True,
                "api_concurrency_limit": 1,
                "reason": "memory pressure",
            },
        )
        result = await runtime.autotune_api_concurrency(parser)

        assert result == {
            "changed": False,
            "old_limit": 1,
            "new_limit": 1,
            "reason": "execution_control_paused",
        }
        assert parser.api_limiter.limit == 1

    asyncio.run(exercise())
