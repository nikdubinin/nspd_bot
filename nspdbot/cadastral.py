"""Распознавание и нормализация кадастровых номеров.

Формат кадастрового номера: АА:ВВ:CCCCCCC:КК, где
АА — кадастровый округ, ВВ — район, CCCCCCC — квартал (обычно 6–7 цифр),
КК — номер объекта в квартале.
"""

from __future__ import annotations

import re

# Номер с двоеточиями (допускаем пробелы вокруг двоеточий — они убираются заранее).
_COLON_RE = re.compile(r"(?<![\d:])(\d{2}):(\d{2}):(\d{1,7}):(\d{1,9})(?![\d:])")
_SPACES_AROUND_COLON_RE = re.compile(r"\s*:\s*")
_DIGIT_GROUPS_RE = re.compile(r"\d+")
_CHUNK_SPLIT_RE = re.compile(r"[\n,;]+")

# Длина «склеенного» префикса округ+район+квартал: 2 + 2 + 7.
_PREFIX_LEN = 11


def _is_valid(district: str, region: str, quarter: str, number: str) -> bool:
    return (
        len(district) == 2
        and district != "00"
        and len(region) == 2
        and 1 <= len(quarter) <= 7
        and 1 <= len(number) <= 9
    )


def normalize_kad_number(raw: str) -> str | None:
    """Приводит одну строку к виду АА:ВВ:CCCCCCC:КК или возвращает None.

    Поддерживаемые варианты ввода:
      77:03:0002007:7190, 77 03 0002007 7190, 77-03-0002007-7190,
      77030002007 7190, 770300020077190.
    """
    if not raw:
        return None

    text = _SPACES_AROUND_COLON_RE.sub(":", raw.strip())
    match = _COLON_RE.search(text)
    if match:
        parts = match.groups()
        return ":".join(parts) if _is_valid(*parts) else None

    groups = _DIGIT_GROUPS_RE.findall(text)
    if len(groups) == 4 and _is_valid(*groups):
        return ":".join(groups)

    # Квартал «склеен» с округом и районом: 77030002007 7190
    if len(groups) == 2 and len(groups[0]) == _PREFIX_LEN:
        prefix, number = groups
        parts = (prefix[:2], prefix[2:4], prefix[4:], number)
        return ":".join(parts) if _is_valid(*parts) else None

    # Всё одной строкой цифр: 770300020077190
    if len(groups) == 1 and len(groups[0]) > _PREFIX_LEN:
        digits = groups[0]
        parts = (digits[:2], digits[2:4], digits[4:_PREFIX_LEN], digits[_PREFIX_LEN:])
        return ":".join(parts) if _is_valid(*parts) else None

    return None


def extract_kad_numbers(text: str) -> list[str]:
    """Находит все кадастровые номера в тексте (без дублей, в порядке появления).

    Текст разбивается по строкам/запятым/точкам с запятой. В каждой части
    ищутся номера с двоеточиями (их может быть несколько), а если таких нет —
    часть нормализуется целиком. Так в одном списке можно смешивать форматы.
    """
    if not text:
        return []

    found: list[str] = []
    for chunk in _CHUNK_SPLIT_RE.split(text):
        normalized = _SPACES_AROUND_COLON_RE.sub(":", chunk)
        matches = [":".join(m.groups()) for m in _COLON_RE.finditer(normalized) if _is_valid(*m.groups())]
        if matches:
            found += matches
        elif number := normalize_kad_number(chunk):
            found.append(number)

    return list(dict.fromkeys(found))


def quarter_of(kad_number: str) -> str:
    """77:03:0002007:7190 -> 77:03:0002007."""
    return kad_number.rsplit(":", 1)[0]
