"""Загрузка настроек из переменных окружения / файла .env."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent.parent


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Переменная {name} должна быть целым числом, получено: {raw!r}") from exc


def _parse_allowed(raw: str) -> tuple[frozenset[int], frozenset[str]]:
    """'123, @user, 456' -> ({123, 456}, {'user'})."""
    ids: set[int] = set()
    names: set[str] = set()
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if item.lstrip("-").isdigit():
            ids.add(int(item))
        else:
            names.add(item.lstrip("@").lower())
    return frozenset(ids), frozenset(names)


@dataclass(frozen=True, slots=True)
class Settings:
    token: str
    allowed_ids: frozenset[int] = field(default_factory=frozenset)
    allowed_usernames: frozenset[str] = field(default_factory=frozenset)
    proxy: str | None = None
    nspd_timeout: int = 15
    nspd_retries: int = 3
    nspd_concurrency: int = 4
    cache_ttl_seconds: int = 30 * 60
    max_batch: int = 20
    log_level: str = "INFO"
    data_dir: Path = PROJECT_DIR / "data"
    log_dir: Path = PROJECT_DIR / "logs"

    @property
    def access_restricted(self) -> bool:
        return bool(self.allowed_ids or self.allowed_usernames)

    def is_allowed(self, user_id: int | None, username: str | None) -> bool:
        if not self.access_restricted:
            return True
        if user_id is not None and user_id in self.allowed_ids:
            return True
        return bool(username) and username.lower() in self.allowed_usernames


def load_settings() -> Settings:
    load_dotenv(PROJECT_DIR / ".env")

    token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "Не задан TELEGRAM_BOT_TOKEN. Создайте файл .env на основе config.example.env"
        )

    allowed_ids, allowed_names = _parse_allowed(os.getenv("ALLOWED_USERS", ""))

    return Settings(
        token=token,
        allowed_ids=allowed_ids,
        allowed_usernames=allowed_names,
        proxy=os.getenv("TELEGRAM_PROXY", "").strip() or None,
        nspd_timeout=max(3, _int_env("NSPD_TIMEOUT", 15)),
        nspd_retries=max(1, _int_env("NSPD_RETRIES", 3)),
        nspd_concurrency=max(1, _int_env("NSPD_CONCURRENCY", 4)),
        cache_ttl_seconds=max(0, _int_env("CACHE_TTL_MINUTES", 30)) * 60,
        max_batch=max(1, _int_env("MAX_BATCH", 20)),
        log_level=os.getenv("LOG_LEVEL", "INFO").strip().upper() or "INFO",
    )
