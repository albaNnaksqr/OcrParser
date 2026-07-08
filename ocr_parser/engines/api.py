from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

T = TypeVar("T")


async def create_chat_completion(parser: Any, **kwargs: Any):
    """Run an OpenAI-compatible chat completion behind the shared API lane."""
    if getattr(parser, "api_limiter", None) is None and getattr(parser, "api_semaphore", None) is None:
        return await parser.client.chat.completions.create(**kwargs)

    from ocr_parser import runtime as runtime_ops

    async with runtime_ops.api_lane(parser):
        return await parser.client.chat.completions.create(**kwargs)


async def run_in_encode_lane(parser: Any, work: Callable[[], Awaitable[T]]) -> T:
    """Run image payload encoding behind the shared encode lane when present."""
    semaphore = getattr(parser, "encode_semaphore", None)
    if semaphore is None:
        return await work()

    async with semaphore:
        return await work()


async def prepare_image_payload(parser: Any, image: Any) -> tuple[str, float]:
    from dots_ocr.model.inference_async import prepare_image_payload_for_vllm

    return await run_in_encode_lane(parser, lambda: prepare_image_payload_for_vllm(image))
