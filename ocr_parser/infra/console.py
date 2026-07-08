from __future__ import annotations

VERBOSE_MODE = False


def set_verbose_mode(enabled: bool) -> None:
    global VERBOSE_MODE
    VERBOSE_MODE = enabled


def console_write(message: str, level: str = "info") -> None:
    if level in ("always", "error", "warning"):
        print(message)
        return
    if VERBOSE_MODE:
        print(message)
