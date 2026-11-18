import secrets
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./noparking.db"
    storage_dir: Path = Path("./storage")

    # Auth. Leave empty in development: a key is generated and persisted under storage_dir.
    secret_key: str = ""
    access_token_minutes: int = 720
    admin_email: str = "admin@local"
    admin_password: str = ""  # empty -> random password generated and printed once at first start

    # Business rules (fixed by the project requirements)
    violation_threshold_seconds: int = 900  # violation only when elapsed is STRICTLY greater
    presence_grace_seconds: int = 60  # sensor/heartbeat silence tolerated before session closes
    anpr_confidence_threshold: float = 0.75
    max_image_bytes: int = 5 * 1024 * 1024

    # SMS queue
    sms_lease_seconds: int = 120
    sms_max_attempts: int = 3
    sms_retry_backoff_seconds: int = 60

    site_label: str = "No-Parking Zone"
    cam_view_token: str = "nopark-view-2026"  # must equal WEB_TOKEN in the firmware config.h
    scheduler_enabled: bool = True
    scheduler_interval_seconds: int = 5
    cors_origins: list[str] = ["http://localhost:5173", "http://127.0.0.1:5173"]


@lru_cache
def get_settings() -> Settings:
    return Settings()


def get_secret_key(s: Settings) -> str:
    if s.secret_key:
        return s.secret_key
    s.storage_dir.mkdir(parents=True, exist_ok=True)
    f = s.storage_dir / ".secret_key"
    if f.exists():
        return f.read_text().strip()
    key = secrets.token_urlsafe(48)
    f.write_text(key)
    return key
