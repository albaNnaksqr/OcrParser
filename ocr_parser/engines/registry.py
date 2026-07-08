from __future__ import annotations

from typing import Any


def create_engine(parser: Any, engine_name: str):
    if engine_name == "dotsocr":
        from .dotsocr import DotsOCREngine

        return DotsOCREngine(parser)
    if engine_name == "mineru":
        from .native_openai import NativeOpenAIEngine

        return NativeOpenAIEngine(parser, engine_name)
    if engine_name == "paddleocr-vl":
        from .paddleocr_vl import PaddleOCRVLEngine

        return PaddleOCRVLEngine(parser)
    raise ValueError(f"Unsupported OCR engine: {engine_name}")
