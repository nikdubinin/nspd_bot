"""Читаемый журнал действий для консоли: кто пишет боту, что запрашивает, чем закончилось.

Каждое событие — одна строка вида «14:05:31  ЗАПРОС    Иван (@ivan) → 77:01:…».
В консоль выводятся только эти строки и предупреждения/ошибки; подробный журнал
со всеми деталями и трассировками по-прежнему пишется в logs/bot.log.
"""

from __future__ import annotations

import logging
import re
import time
from collections import Counter
from contextvars import ContextVar
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram import Chat, Update, User

    from .models import NspdObject

ACTIVITY_LOGGER = "nspdbot.activity"
LABEL_WIDTH = 9

logger = logging.getLogger(ACTIVITY_LOGGER)

# Кто сейчас обращается к боту. Задаётся в начале обработки каждого обновления
# (у каждого обновления своя asyncio-задача, поэтому значения не смешиваются).
_current_user: ContextVar[str] = ContextVar("nspd_current_user", default="—")


def format_duration(seconds: float) -> str:
    """«45 с», «4 мин 12 с», «1 ч 5 мин»."""
    s = max(0, round(seconds))
    if s < 60:
        return f"{s} с"
    m = s // 60
    if m < 60:
        return f"{m} мин {s % 60} с"
    return f"{m // 60} ч {m % 60} мин"


def _seconds(value: float) -> str:
    return f"{value:.1f} с".replace(".", ",")


def _short(text: str | None, limit: int = 70) -> str:
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


# Служебные части адреса, которые в консоли только мешают читать
_ADDRESS_NOISE = re.compile(
    r"^(Российская Федерация|РФ),\s*|вн\.\s?тер\.\s?г\.\s*|муниципальный округ\s*|городской округ\s*",
    re.IGNORECASE,
)


def _address(text: str | None) -> str:
    return _short(_ADDRESS_NOISE.sub("", text or ""), 60)


def _numbers(numbers: list[str], limit: int = 5) -> str:
    shown = ", ".join(numbers[:limit])
    return shown + (f" и ещё {len(numbers) - limit}" if len(numbers) > limit else "")


def user_label(user: User | None, chat: Chat | None = None) -> str:
    """«Иван Петров (@ivan)», «Иван (id 123)», плюс «в „Чат“» для групп."""
    if user is None:
        return "—"
    name = " ".join(part for part in (user.first_name, user.last_name) if part) or "без имени"
    label = f"{name} (@{user.username})" if user.username else f"{name} (id {user.id})"
    if chat is not None and chat.type != "private" and chat.title:
        label += f" в «{chat.title}»"
    return label


class Activity:
    """Пишет события в журнал и ведёт счётчики с момента запуска."""

    def __init__(self) -> None:
        self.started = time.monotonic()
        self.stats: Counter[str] = Counter()
        self._users: set[int] = set()
        self._last_day = ""

    # ------------------------------------------------------------------ helpers

    @property
    def who(self) -> str:
        return _current_user.get()

    def emit(self, label: str, text: str, level: int = logging.INFO) -> None:
        logger.log(level, "%s %s", label.ljust(LABEL_WIDTH), text)

    # ------------------------------------------------------------------ события

    def track(self, update: Update) -> str:
        """Запоминает, кто прислал обновление; новых за время работы отмечает отдельно."""
        user = update.effective_user
        label = user_label(user, update.effective_chat)
        _current_user.set(label)
        if user is not None and user.id not in self._users:
            self._users.add(user.id)
            id_note = "" if f"id {user.id}" in label else f" · id {user.id}"
            self.emit("НОВЫЙ", f"{label}{id_note} · впервые с момента запуска")

        message = update.message
        if message is not None and message.text and message.text.startswith("/"):
            self.emit("команда", f"{label}: {_short(message.text, 60)}")
        return label

    def startup(self, username: str, version: str, restricted: int) -> None:
        access = f"доступ ограничен ({restricted})" if restricted else "доступ открыт всем"
        self.emit("ЗАПУСК", f"бот @{username} v{version} запущен · {datetime.now():%d.%m.%Y} · {access}")

    def lookup(
        self,
        kad_number: str,
        *,
        obj: NspdObject | None = None,
        not_found: bool = False,
        seconds: float = 0.0,
        cached: bool = False,
        refresh: bool = False,
    ) -> None:
        self.stats["lookups"] += 1
        timing = "из кэша" if cached else _seconds(seconds)
        note = " · обновление" if refresh else ""
        if obj is not None:
            self.stats["found"] += 1
            details = " · ".join(p for p in (obj.type_label, _address(obj.address)) if p)
            self.emit("ЗАПРОС", f"{self.who} → {kad_number} · {details} · {timing}{note}")
        elif not_found:
            self.stats["not_found"] += 1
            self.emit("ЗАПРОС", f"{self.who} → {kad_number} · не найден в НСПД · {timing}{note}")
        else:
            self.stats["failed"] += 1
            self.emit("СБОЙ", f"{self.who} → {kad_number} · НСПД не ответила · {timing}", logging.WARNING)

    def batch(self, numbers: list[str], found: int, seconds: float) -> None:
        self.stats["batches"] += 1
        self.emit(
            "ПАКЕТ",
            f"{self.who} → {_numbers(numbers)} · найдено {found} из {len(numbers)} · "
            f"{_seconds(seconds)} · отправлен CSV",
        )

    def offer(self, title: str, numbers: list[str], filled: str = "") -> None:
        self.stats["offers"] += 1
        note = f" · анкета {filled}" if filled else ""
        self.emit("ПРЕДЛОЖ", f"{self.who}: {title} · {_numbers(numbers)}{note}")

    def pledge(self, numbers: list[str], found: int, seconds: float) -> None:
        self.emit(
            "ЗАЛОГ",
            f"{self.who}: займ под залог · {_numbers(numbers)} · в предложении {found} из {len(numbers)} · "
            f"{_seconds(seconds)}",
        )

    def action(self, text: str) -> None:
        self.emit("кнопка", f"{self.who}: {text}")

    def unrecognized(self, text: str) -> None:
        self.emit("текст", f"{self.who}: «{_short(text, 50)}» · номер не распознан")

    def denied(self) -> None:
        self.stats["denied"] += 1
        self.emit("ДОСТУП", f"{self.who}: отказано, нет в ALLOWED_USERS", logging.WARNING)

    def error(self, error: BaseException | None) -> None:
        self.stats["errors"] += 1
        text = f"{type(error).__name__}: {error}" if error else "неизвестная ошибка"
        self.emit("ОШИБКА", f"{self.who}: {_short(text, 300)}", logging.ERROR)

    # ------------------------------------------------------------------ сводки

    def summary(self) -> str:
        s = self.stats
        parts = [
            f"работаю {format_duration(time.monotonic() - self.started)}",
            f"пользователей {len(self._users)}",
            f"запросов {s['lookups']} (найдено {s['found']}, не найдено {s['not_found']}, сбоев {s['failed']})",
            f"пакетов {s['batches']}",
            f"предложений {s['offers']}",
            f"ошибок {s['errors']}",
        ]
        if s["denied"]:
            parts.append(f"отказов в доступе {s['denied']}")
        return " · ".join(parts)

    def heartbeat(self, now: datetime | None = None) -> None:
        """Строка раз в час: видно, что бот жив, даже если запросов нет."""
        day = f"{now or datetime.now():%d.%m.%Y}"
        note = f" · {day}" if day != self._last_day else ""
        self._last_day = day
        self.emit("СВОДКА", self.summary() + note)

    def shutdown(self) -> None:
        self.emit("ОСТАНОВКА", self.summary())


class ConsoleFormatter(logging.Formatter):
    """«14:05:31  ЗАПРОС    …» для журнала действий, «14:05:31  ВНИМАНИЕ  …» для прочих."""

    LEVEL_LABELS = {logging.WARNING: "ВНИМАНИЕ", logging.ERROR: "ОШИБКА", logging.CRITICAL: "ОШИБКА"}

    def format(self, record: logging.LogRecord) -> str:
        clock = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
        message = record.getMessage()
        if record.name == ACTIVITY_LOGGER:
            return f"{clock}  {message}"
        label = self.LEVEL_LABELS.get(record.levelno, record.levelname).ljust(LABEL_WIDTH)
        if record.exc_info and record.exc_info[1] is not None:
            # Полная трассировка — только в файле, в консоли — суть
            exc = record.exc_info[1]
            message = f"{message}: {type(exc).__name__}: {exc}"
        return f"{clock}  {label} {_short(message, 300)}"


class ConsoleFilter(logging.Filter):
    """В консоль — журнал действий и всё от WARNING; детали (console=False) — только в файл."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not getattr(record, "console", True):
            return False
        return record.name == ACTIVITY_LOGGER or record.levelno >= logging.WARNING
