from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


DEFAULT_DATABASE_URL = "sqlite:///./ocr_platform.db"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def env_truthy(environment: Mapping[str, str], name: str) -> bool:
    return environment.get(name, "").strip().lower() in TRUTHY_ENV_VALUES


def _parse_port(environment: Mapping[str, str]) -> int:
    raw_value = environment.get("OCR_PLATFORM_PORT", str(DEFAULT_PORT))
    try:
        return int(raw_value)
    except ValueError:
        raise ValueError("OCR_PLATFORM_PORT must be an integer") from None


@dataclass(frozen=True, slots=True)
class ControlSettings:
    database_url: str = field(default=DEFAULT_DATABASE_URL, repr=False)
    require_postgres: bool = False
    auto_migrate: bool = False
    require_current_migrations: bool = False
    host: str = DEFAULT_HOST
    port: int = DEFAULT_PORT
    api_token: str | None = field(default=None, repr=False)
    require_api_token: bool = False
    enable_remote_admin: bool = False
    saved_model_profile_keys_allowed: bool = False

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "ControlSettings":
        values = os.environ if environment is None else environment
        saved_keys_disabled = env_truthy(
            values,
            "OCR_PLATFORM_DISABLE_SAVED_MODEL_PROFILE_KEYS",
        )
        return cls(
            database_url=(
                values.get("OCR_PLATFORM_DATABASE_URL") or DEFAULT_DATABASE_URL
            ),
            require_postgres=env_truthy(
                values,
                "OCR_PLATFORM_REQUIRE_POSTGRES",
            ),
            auto_migrate=(
                values.get("OCR_PLATFORM_AUTO_MIGRATE", "").strip() == "1"
            ),
            require_current_migrations=env_truthy(
                values,
                "OCR_PLATFORM_REQUIRE_CURRENT_MIGRATIONS",
            ),
            host=values.get("OCR_PLATFORM_HOST", DEFAULT_HOST),
            port=_parse_port(values),
            api_token=values.get("OCR_PLATFORM_API_TOKEN") or None,
            require_api_token=env_truthy(
                values,
                "OCR_PLATFORM_REQUIRE_API_TOKEN",
            ),
            enable_remote_admin=env_truthy(
                values,
                "OCR_PLATFORM_ENABLE_REMOTE_ADMIN",
            ),
            saved_model_profile_keys_allowed=(
                not saved_keys_disabled
                and env_truthy(
                    values,
                    "OCR_PLATFORM_ALLOW_SAVED_MODEL_PROFILE_KEYS",
                )
            ),
        )


__all__ = [
    "ControlSettings",
    "DEFAULT_DATABASE_URL",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "TRUTHY_ENV_VALUES",
    "env_truthy",
]
