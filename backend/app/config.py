"""Environment configuration.

Every secret here is server-side only and must never reach the browser bundle
(CLAUDE.md, plan.md §11). Nothing in this module is exported to the frontend.
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Runtime ---
    port: int = 8080
    log_level: str = "info"

    # SQLite path. On Fly this is /data/ticker.db on the mounted volume; locally
    # it defaults beside the backend package.
    db_path: Path = Path("./ticker.db")

    # Directory holding the built Vite output. The Dockerfile copies
    # /web/dist to /app/static, so the default is correct in the image.
    static_dir: Path = Path("static")

    # --- Secrets (fly secrets set ...) ---
    # Optional through Phase 0 so the app boots before the keys exist. The
    # services that need them (Phase 1+) must fail loudly at their own call
    # sites rather than letting the whole app refuse to start — a boot-crash
    # loop takes the health check down with it (plan.md §9.3).
    anthropic_api_key: str | None = None
    finnhub_api_key: str | None = None
    session_secret: str | None = None
    app_passphrase: str | None = None

    anthropic_model: str = "claude-sonnet-5"

    # Name of the active market data provider, reported by /api/health.
    provider: str = "finnhub"


@lru_cache
def get_settings() -> Settings:
    return Settings()
