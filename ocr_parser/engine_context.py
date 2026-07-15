from __future__ import annotations

from dataclasses import fields
from typing import Any

from .config import ParserConfig


class ParserEngineContext:
    """Explicit adapter between engine plugins and parser implementation details.

    Engines may read normalized configuration, runtime resources, and the small
    set of domain/output callbacks below. They cannot reach document orchestration
    or the parser facade itself.
    """

    _SERVICE_NAMES = {
        "NonStandardModelOutputError",
        "_console_write",
        "_maybe_refine_table_blocks",
        "_merge_adjacent_text_blocks_in_same_page",
        "_run_inference_with_retries",
        "_save_intermediate_outputs_async",
        "_is_transient_inference_error",
        "_trim_first_page_blocks",
        "_validate_cells_structure",
        "get_prompt",
        "record_api_error",
    }

    def __init__(self, facade: Any) -> None:
        object.__setattr__(self, "_facade", facade)
        object.__setattr__(self, "config", facade.config)
        object.__setattr__(self, "runtime", facade.runtime)
        object.__setattr__(
            self,
            "_config_names",
            frozenset(item.name for item in fields(ParserConfig)),
        )

    def __getattr__(self, name: str) -> Any:
        runtime = object.__getattribute__(self, "runtime")
        if name in vars(runtime):
            return vars(runtime)[name]

        if name in object.__getattribute__(self, "_config_names"):
            return getattr(object.__getattribute__(self, "config"), name)

        if name in self._SERVICE_NAMES:
            return getattr(object.__getattribute__(self, "_facade"), name)
        raise AttributeError(f"engine context does not expose {name!r}")

    def __setattr__(self, name: str, value: Any) -> None:
        if name in {"_facade", "config", "runtime", "_config_names"}:
            object.__setattr__(self, name, value)
            return
        setattr(object.__getattribute__(self, "runtime"), name, value)
