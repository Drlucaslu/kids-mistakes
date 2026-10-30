"""Settings from the environment. Everything lives under DATA_DIR (the Olares app-data volume)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path


@dataclass
class Settings:
    data_dir: Path = field(default_factory=lambda: Path(os.environ.get("DATA_DIR", "./data")).resolve())
    timezone: str = field(default_factory=lambda: os.environ.get("TIMEZONE", "Asia/Singapore"))
    # Olares gateway sets this header for the signed-in owner; it strips it from outside requests.
    trusted_user_header: str = field(default_factory=lambda: os.environ.get("TRUSTED_USER_HEADER", "X-Bfl-User"))
    init_parent_name: str = field(default_factory=lambda: os.environ.get("INIT_PARENT_NAME", "Parent"))
    init_parent_pin: str = field(default_factory=lambda: os.environ.get("INIT_PARENT_PIN", ""))
    # AI defaults; the Settings page can override provider/model/base_url at runtime. Keys stay in env.
    model_provider: str = field(default_factory=lambda: os.environ.get("MODEL_PROVIDER", ""))  # anthropic | openai_compatible | mock | ""
    model_name: str = field(default_factory=lambda: os.environ.get("MODEL_NAME", ""))
    model_base_url: str = field(default_factory=lambda: os.environ.get("MODEL_BASE_URL", ""))
    anthropic_api_key: str = field(default_factory=lambda: os.environ.get("ANTHROPIC_API_KEY", ""))
    model_api_key: str = field(default_factory=lambda: os.environ.get("MODEL_API_KEY", ""))  # for openai_compatible
    port: int = field(default_factory=lambda: int(os.environ.get("PORT", "8080")))

    @property
    def db_path(self) -> Path:
        return self.data_dir / "cuoti.db"

    @property
    def photo_dir(self) -> Path:
        return self.data_dir / "photos"

    @property
    def backup_dir(self) -> Path:
        return self.data_dir / "backups"


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    for d in (s.data_dir, s.photo_dir, s.backup_dir):
        d.mkdir(parents=True, exist_ok=True)
    return s
