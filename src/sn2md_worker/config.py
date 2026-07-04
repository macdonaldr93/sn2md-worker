from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]


class DriveConfig(BaseModel, frozen=True):
    source_folder_id: str = ""
    poll_debounce_stable_seconds: int = Field(default=30, ge=1)
    poll_debounce_interval_seconds: int = Field(default=10, ge=1)
    poll_debounce_max_iterations: int = Field(default=60, ge=1)
    watch_channel_ttl_days: int = Field(default=6, ge=1, le=7)
    fallback_poll_cron: str = "*/5 * * * *"


class VaultConfig(BaseModel, frozen=True):
    root_path: Path = Path("/vault")
    mirror_source_layout: bool = True


class Sn2mdConfig(BaseModel, frozen=True):
    model: str = "gemini/gemini-2.5-pro"
    api_key: SecretStr | None = None


class QueueConfig(BaseModel, frozen=True):
    convert_concurrency: int = Field(default=2, ge=1)
    convert_rate_limit_per_minute: int = Field(default=30, ge=1)
    debounce_concurrency: int = Field(default=8, ge=1)


class ObservabilityConfig(BaseModel, frozen=True):
    log_level: LogLevel = "INFO"
    status_endpoint_enabled: bool = True


class DatabaseConfig(BaseModel, frozen=True):
    """SQLite/Postgres URL. Serves both DBOS system state and our own tables."""

    url: str = "sqlite:///./data/sn2md-worker.sqlite"


class WebhookConfig(BaseModel, frozen=True):
    url: str = ""


class GoogleConfig(BaseModel, frozen=True):
    application_credentials: Path = Path("/secrets/service-account.json")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SN2MD_WORKER__",
        env_nested_delimiter="__",
        env_file=".env",
        env_file_encoding="utf-8",
        toml_file="config.toml",
        extra="ignore",
    )

    drive: DriveConfig = DriveConfig()
    vault: VaultConfig = VaultConfig()
    sn2md: Sn2mdConfig = Sn2mdConfig()
    queue: QueueConfig = QueueConfig()
    observability: ObservabilityConfig = ObservabilityConfig()
    database: DatabaseConfig = DatabaseConfig()
    webhook: WebhookConfig = WebhookConfig()
    google: GoogleConfig = GoogleConfig()

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            TomlConfigSettingsSource(settings_cls),
            file_secret_settings,
        )


def load_settings() -> Settings:
    return Settings()


def get_settings() -> Settings:
    """Return the process-wide Settings; raises if not yet initialized."""
    if _Holder.settings is None:
        raise RuntimeError("settings not initialized; call set_settings() at startup")
    return _Holder.settings


def set_settings(settings: Settings) -> None:
    """Install the process-wide Settings. Call once from the entrypoint."""
    _Holder.settings = settings


class _Holder:
    settings: Settings | None = None


__all__ = [
    "DatabaseConfig",
    "DriveConfig",
    "GoogleConfig",
    "LogLevel",
    "ObservabilityConfig",
    "QueueConfig",
    "Settings",
    "Sn2mdConfig",
    "VaultConfig",
    "WebhookConfig",
    "get_settings",
    "load_settings",
    "set_settings",
]
