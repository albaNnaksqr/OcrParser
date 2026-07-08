from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class NativeArtifact:
    engine: str
    kind: str
    path: str


def native_engine_dir(save_dir: str, engine: str) -> Path:
    path = Path(save_dir) / "native" / engine
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_native_json(
    save_dir: str,
    engine: str,
    filename: str,
    payload: Any,
    *,
    kind: str = "json",
) -> NativeArtifact:
    path = native_engine_dir(save_dir, engine) / filename
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return NativeArtifact(engine=engine, kind=kind, path=str(path))


def write_native_text(
    save_dir: str,
    engine: str,
    filename: str,
    content: str,
    *,
    kind: str = "text",
) -> NativeArtifact:
    path = native_engine_dir(save_dir, engine) / filename
    path.write_text(content or "", encoding="utf-8")
    return NativeArtifact(engine=engine, kind=kind, path=str(path))


async def async_write_native_json(
    save_dir: str,
    engine: str,
    filename: str,
    payload: Any,
    *,
    kind: str = "json",
) -> NativeArtifact:
    path = native_engine_dir(save_dir, engine) / filename
    content = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: path.write_text(content, encoding="utf-8"))
    return NativeArtifact(engine=engine, kind=kind, path=str(path))


async def async_write_native_text(
    save_dir: str,
    engine: str,
    filename: str,
    content: str,
    *,
    kind: str = "text",
) -> NativeArtifact:
    path = native_engine_dir(save_dir, engine) / filename
    data = content or ""
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, lambda: path.write_text(data, encoding="utf-8"))
    return NativeArtifact(engine=engine, kind=kind, path=str(path))
