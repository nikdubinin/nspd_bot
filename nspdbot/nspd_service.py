"""Асинхронная обёртка над библиотекой nspd-request: кэш, повторы, лимит параллельности."""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from typing import Any, Generic, TypeVar

from nspd_request import NSPD

from .models import NspdObject, ObjectKind

logger = logging.getLogger(__name__)

T = TypeVar("T")

_NEGATIVE_TTL_SECONDS = 5 * 60


class NspdError(Exception):
    """Базовая ошибка запроса к НСПД."""


class NspdNotFound(NspdError):
    """Объект с таким кадастровым номером в НСПД не найден."""


class NspdUnavailable(NspdError):
    """НСПД не ответила (таймаут, сетевой сбой, 5xx)."""


class TTLCache(Generic[T]):
    """Простой LRU-кэш с временем жизни записей."""

    def __init__(self, ttl: float, max_size: int = 2000) -> None:
        self._ttl = ttl
        self._max_size = max_size
        self._data: OrderedDict[str, tuple[float, T]] = OrderedDict()

    def get(self, key: str) -> T | None:
        item = self._data.get(key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at < time.monotonic():
            del self._data[key]
            return None
        self._data.move_to_end(key)
        return value

    def set(self, key: str, value: T) -> None:
        if self._ttl <= 0:
            return
        self._data[key] = (time.monotonic() + self._ttl, value)
        self._data.move_to_end(key)
        while len(self._data) > self._max_size:
            self._data.popitem(last=False)

    def pop(self, key: str) -> None:
        self._data.pop(key, None)


def _is_not_found(error: str) -> bool:
    text = error.lower()
    return "404" in text or "не найден" in text or "не нашли" in text


class NspdService:
    def __init__(
        self,
        *,
        timeout: int = 15,
        retries: int = 3,
        concurrency: int = 4,
        cache_ttl: int = 30 * 60,
    ) -> None:
        self._client = NSPD(timeout=timeout)
        self._retries = retries
        self._semaphore = asyncio.Semaphore(concurrency)
        self._objects: TTLCache[NspdObject] = TTLCache(cache_ttl)
        self._not_found: TTLCache[bool] = TTLCache(_NEGATIVE_TTL_SECONDS)
        self._related: TTLCache[list[str]] = TTLCache(cache_ttl)
        self._inflight: dict[str, asyncio.Future[NspdObject]] = {}

    # ------------------------------------------------------------------ public

    def is_cached(self, kad_number: str) -> bool:
        """Есть ли свежий ответ в кэше (для журнала: «из кэша»)."""
        return self._objects.get(kad_number) is not None or bool(self._not_found.get(kad_number))

    async def get_object(self, kad_number: str, *, force: bool = False) -> NspdObject:
        """Возвращает объект НСПД. Бросает NspdNotFound / NspdUnavailable."""
        if force:
            self._objects.pop(kad_number)
            self._not_found.pop(kad_number)
            self._related.pop(kad_number)
        else:
            cached = self._objects.get(kad_number)
            if cached is not None:
                return cached
            if self._not_found.get(kad_number):
                raise NspdNotFound(kad_number)

        # Повторные нажатия / одинаковые номера в пачке не дублируют запросы.
        pending = self._inflight.get(kad_number)
        if pending is not None:
            return await asyncio.shield(pending)

        future: asyncio.Future[NspdObject] = asyncio.get_running_loop().create_future()
        self._inflight[kad_number] = future
        try:
            obj = await self._load_object(kad_number)
        except BaseException as exc:
            future.set_exception(exc)
            future.exception()  # помечаем как прочитанное, чтобы не было warning
            raise
        else:
            future.set_result(obj)
            return obj
        finally:
            self._inflight.pop(kad_number, None)

    async def get_related(self, obj: NspdObject) -> list[str]:
        """Связанные объекты: для ЗУ — ОКС на участке, для ОКС — участки под ним."""
        if obj.kind is ObjectKind.PREMISES or obj.kind is ObjectKind.PARKING:
            return [obj.parent_cad_number] if obj.parent_cad_number else []
        if not obj.has_related_lookup:
            return []

        cached = self._related.get(obj.cad_number)
        if cached is not None:
            return cached

        method = (
            self._client.get_oks_by_land_plot
            if obj.kind is ObjectKind.LAND
            else self._client.get_land_plots_by_oks
        )
        async with self._semaphore:
            raw = await asyncio.to_thread(method, obj.geom_id)

        related = self._normalize_related(raw, exclude=obj.cad_number)
        self._related.set(obj.cad_number, related)
        return related

    # ------------------------------------------------------------------ internals

    async def _load_object(self, kad_number: str) -> NspdObject:
        last_error = "нет ответа"
        for attempt in range(1, self._retries + 1):
            try:
                async with self._semaphore:
                    data: Any = await asyncio.to_thread(
                        self._client.search_by_cadastral_number, kad_number
                    )
            except Exception as exc:  # JSON-ошибки и пр., которые библиотека не ловит
                data = {"error": f"{type(exc).__name__}: {exc}"}

            if isinstance(data, dict) and not data.get("error"):
                obj = NspdObject.from_response(data, kad_number)
                self._objects.set(kad_number, obj)
                return obj

            last_error = str(data.get("error") if isinstance(data, dict) else data)
            if _is_not_found(last_error):
                self._not_found.set(kad_number, True)
                raise NspdNotFound(kad_number)

            logger.warning(
                "НСПД: попытка %d/%d для %s не удалась: %s",
                attempt, self._retries, kad_number, last_error,
            )
            if attempt < self._retries:
                await asyncio.sleep(attempt * 1.5)

        raise NspdUnavailable(last_error)

    @staticmethod
    def _normalize_related(raw: Any, *, exclude: str) -> list[str]:
        if not raw:
            return []
        result: list[str] = []
        for item in raw if isinstance(raw, list) else [raw]:
            if isinstance(item, dict):
                item = item.get("cad_num") or item.get("label") or item.get("value") or item.get("descr")
            if item:
                value = str(item).strip()
                if value and value != exclude:
                    result.append(value)
        return list(dict.fromkeys(result))
