"""Форматирование сведений НСПД в сообщения Telegram (parse_mode=HTML)."""

from __future__ import annotations

import csv
import html
import io
from datetime import datetime
from typing import Any, Literal
from urllib.parse import quote

from .models import NspdObject, ObjectKind

OfferKind = Literal["loan", "buyout"]

TELEGRAM_TEXT_LIMIT = 4096

RIGHTS_NOTE = (
    "<i>Сведения о правообладателях и обременениях в открытом доступе НСПД не публикуются — "
    "для них нужна выписка из ЕГРН.</i>"
)


# ------------------------------------------------------------------------------
# Базовые помощники
# ------------------------------------------------------------------------------


def esc(value: object) -> str:
    return "" if value is None else html.escape(str(value), quote=False)


def truncate(value: object, max_len: int = 600) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, set)):
        text = "; ".join(str(x) for x in value if x is not None)
    else:
        text = str(value)
    text = text.strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def fmt_number(value: Any) -> str:
    """1234567.8 -> '1 234 567,8'."""
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    text = f"{num:,.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    elif text.endswith("0"):
        text = text[:-1]
    return text.replace(",", " ").replace(".", ",")


def fmt_date(value: Any) -> str:
    """'2024-01-01' / '2024-03-12T00:00:00Z' -> '01.01.2024'."""
    if not value:
        return ""
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d.%m.%Y")
    except ValueError:
        return text


def fmt_datetime(value: Any) -> str:
    if not value:
        return ""
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return text


def _kv(label: str, value: Any, *, code: bool = False, suffix: str = "") -> str | None:
    if value is None or value == "":
        return None
    shown = f"<code>{esc(value)}</code>" if code else esc(value)
    return f"• <b>{esc(label)}:</b> {shown}{suffix}"


def _area(obj: NspdObject) -> str | None:
    return f"{fmt_number(obj.area)} кв. м" if obj.area else None


def _money(value: float | None) -> str | None:
    return f"{fmt_number(value)} руб." if value is not None else None


def _floors(obj: NspdObject) -> str | None:
    if not obj.floors:
        return None
    if obj.underground_floors:
        return f"{obj.floors} (подземных: {obj.underground_floors})"
    return str(obj.floors)


def _compact(lines: list[str | None]) -> list[str]:
    return [line for line in lines if line]


def fit_message(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Страховка от превышения лимита Telegram (обрезаем по строкам, не ломая HTML-теги)."""
    if len(text) <= limit:
        return text
    tail = "\n…"
    result: list[str] = []
    size = 0
    for line in text.split("\n"):
        if size + len(line) + 1 + len(tail) > limit:
            break
        result.append(line)
        size += len(line) + 1
    return "\n".join(result) + tail


# ------------------------------------------------------------------------------
# Ссылки
# ------------------------------------------------------------------------------


def nspd_url(kad_number: str) -> str:
    return f"https://nspd.gov.ru/map?thematic=PKK&query={quote(kad_number)}"


def yandex_maps_url(obj: NspdObject) -> str | None:
    if obj.centroid:
        lat, lon = obj.centroid
        return f"https://yandex.ru/maps/?pt={lon},{lat}&z=18&l=map"
    if obj.address:
        return f"https://yandex.ru/maps/?text={quote(obj.address)}"
    return None


# ------------------------------------------------------------------------------
# Карточка объекта
# ------------------------------------------------------------------------------


def format_card(obj: NspdObject, *, short: bool = False) -> str:
    header = _compact([
        f"<b>{esc(obj.type_label)}</b>",
        f"<b>Кадастровый номер:</b> <code>{esc(obj.cad_number)}</code>",
        f"<b>Адрес:</b> {esc(obj.address)}" if obj.address else None,
    ])

    if short:
        lines = header + _compact([
            _kv("Площадь", _area(obj)),
            _kv("Назначение", obj.purpose),
            _kv("Категория земель", obj.land_category),
            _kv("Этаж", obj.floor),
            _kv("Кадастровая стоимость", _money(obj.cost_value)),
        ])
        lines.append("\n<i>Подробный формат — команда /full</i>")
        return fit_message("\n".join(lines))

    lines = list(header)
    if obj.name and obj.name.lower() != (obj.purpose or "").lower():
        lines.append(f"<b>Наименование:</b> {esc(truncate(obj.name, 300))}")

    characteristics = _compact([
        _kv("Площадь", _area(obj)),
        _kv("Категория земель", obj.land_category),
        _kv("Назначение", obj.purpose),
        _kv("Вид объекта", obj.subtype if obj.subtype and obj.subtype.lower() not in obj.type_label.lower() else None),
        _kv("Этаж", obj.floor),
        _kv("Этажность", _floors(obj)),
        _kv("Материал стен", obj.materials),
        _kv("Год постройки", obj.year_built),
        _kv("Год ввода в эксплуатацию", obj.year_commissioning),
        _kv("Статус", obj.status),
        _kv("Дата постановки на учёт", fmt_date(obj.registration_date)),
        _kv("Форма собственности", obj.ownership_type),
        _kv("Вид права", obj.right_type),
        _kv("Объект культурного наследия", obj.cultural_heritage),
        _kv("Кадастровый квартал", obj.quarter, code=True),
        _kv("Расположено в здании", obj.parent_cad_number, code=True),
        _kv("Входит в состав", obj.facility_cad_number, code=True),
    ])
    lines.append("\n<b>Характеристики</b>")
    lines.extend(characteristics or ["• нет сведений в открытых данных НСПД"])

    if obj.permitted_use:
        lines.append("\n<b>Вид разрешённого использования</b>")
        lines.append(esc(truncate(obj.permitted_use, 700)))

    if obj.united_cad_numbers:
        lines.append("\n<b>Единое землепользование / объединённые номера</b>")
        lines.append(esc(truncate(obj.united_cad_numbers, 400)))
    if obj.intersected_cad_numbers:
        lines.append("\n<b>Пересечения</b>")
        lines.append(esc(truncate(obj.intersected_cad_numbers, 400)))

    if obj.cost_value is not None:
        lines.append("\n<b>Кадастровая стоимость</b>")
        lines.extend(_compact([
            _kv("Стоимость", _money(obj.cost_value)),
            _kv("Удельный показатель", f"{fmt_number(obj.cost_index)} руб./кв. м" if obj.cost_index else None),
            _kv("Дата определения", fmt_date(obj.cost_determination_date)),
            _kv("Применяется с", fmt_date(obj.cost_application_date)),
            _kv("Дата внесения", fmt_date(obj.cost_registration_date)),
        ]))

    if obj.updated_at:
        lines.append(f"\n<i>Обновлено в НСПД: {esc(fmt_datetime(obj.updated_at))}</i>")
    lines.append(RIGHTS_NOTE)

    return fit_message("\n".join(lines))


def format_related(obj: NspdObject, related: list[str], *, limit: int = 50) -> str:
    if not related:
        return f"Связанные объекты для <code>{esc(obj.cad_number)}</code> в открытых данных НСПД не найдены."

    title = "Здание, в котором расположен объект" if obj.parent_cad_number in related else "Связанные объекты"
    lines = [f"<b>{title}</b> для <code>{esc(obj.cad_number)}</code> ({len(related)}):"]
    lines.extend(f"• <code>{esc(cad)}</code>" for cad in related[:limit])
    if len(related) > limit:
        lines.append(f"• …и ещё {len(related) - limit}")
    lines.append("\n<i>Нажмите на номер, чтобы скопировать, или на кнопку ниже, чтобы открыть карточку.</i>")
    return fit_message("\n".join(lines))


# ------------------------------------------------------------------------------
# Предложения для инвесторов
# ------------------------------------------------------------------------------

_BLANK = "__________________"

Answers = dict[str, str] | None


def _field(answers: Answers, key: str, label: str) -> str:
    """«• Срок: 12 месяцев» или пустая графа, если ответа нет."""
    value = (answers or {}).get(key)
    return f"• {label}: {esc(value) if value else _BLANK}"


def _loan_terms(answers: Answers) -> list[str]:
    return [
        "<b>Параметры займа</b>",
        _field(answers, "amount", "Требуемая сумма"),
        _field(answers, "term", "Срок"),
        _field(answers, "purpose", "Цель"),
        "",
        "<b>Информация о заёмщике</b>",
        _field(answers, "job", "Место работы / должность"),
        _field(answers, "income", "Ежемесячный доход"),
        _field(answers, "business", "Наличие ИП / СЗ / ООО"),
    ]


def _additional(answers: Answers, *, has_premises: bool, documents: str) -> list[str]:
    return [
        "<b>Дополнительно</b>",
        _field(answers, "encumbrances", "Обременения"),
        *([_field(answers, "registered", "Зарегистрированные лица")] if has_premises else []),
        documents,
        "",
        "Ждём ваших предложений и вопросов.",
    ]


def _offer_characteristics(obj: NspdObject) -> list[str]:
    characteristics = _compact([
        f"• Площадь: {esc(_area(obj))}" if obj.area else None,
        f"• Категория земель: {esc(obj.land_category)}" if obj.land_category else None,
        f"• Назначение: {esc(obj.purpose)}" if obj.purpose else None,
        f"• Вид разрешённого использования: {esc(truncate(obj.permitted_use, 300))}" if obj.permitted_use else None,
        f"• Этаж: {esc(obj.floor)}" if obj.floor else None,
        f"• Этажность: {esc(_floors(obj))}" if obj.floors else None,
        f"• Материал стен: {esc(obj.materials)}" if obj.materials else None,
        f"• Год постройки / ввода: {esc(obj.year_built or obj.year_commissioning)}"
        if (obj.year_built or obj.year_commissioning) else None,
        f"• Кадастровая стоимость: {esc(_money(obj.cost_value))}" if obj.cost_value is not None else None,
    ])
    return characteristics or ["• Характеристики в открытых данных НСПД не заполнены."]


def format_offer(obj: NspdObject, kind: OfferKind, answers: Answers = None) -> str:
    noun, located = obj.noun_phrase()

    title = "Предложение по займу под залог" if kind == "loan" else "Предложение на срочный выкуп"
    lines = [f"<b>{title}</b>", "", "Добрый день, уважаемые коллеги!", ""]

    intro = f"Вашему вниманию предлагается {esc(noun)} с кадастровым номером <code>{esc(obj.cad_number)}</code>"
    intro += f", {located} по адресу: {esc(obj.address)}." if obj.address else "."
    lines += [intro, "", "<b>Характеристики объекта</b>"]
    lines.extend(_offer_characteristics(obj))

    if kind == "loan":
        lines += ["", *_loan_terms(answers)]
        documents = "• Документы и фотографии предмета залога прилагаются."
    else:
        lines += [
            "",
            "<b>Параметры сделки</b>",
            _field(answers, "price", "Ожидаемая цена выкупа"),
            _field(answers, "deadline", "Желаемый срок сделки"),
        ]
        documents = "• Документы и фотографии объекта прилагаются."

    lines += ["", *_additional(answers, has_premises=obj.kind is ObjectKind.PREMISES, documents=documents)]
    return fit_message("\n".join(lines))


def format_pledge_offer(objects: list[NspdObject], answers: Answers = None) -> list[str]:
    """Предложение по займу под залог нескольких объектов.

    Возвращает список сообщений: длинный текст делится по границам объектов,
    чтобы не превысить лимит Telegram.
    """
    if len(objects) == 1:
        return [format_offer(objects[0], "loan", answers)]

    blocks = [
        "\n".join([
            "<b>Предложение по займу под залог</b>",
            "",
            "Добрый день, уважаемые коллеги!",
            "",
            f"Вашему вниманию предлагается займ под залог {len(objects)} объектов недвижимости.",
        ])
    ]

    for index, obj in enumerate(objects, 1):
        noun, located = obj.noun_phrase()
        head = f"<b>Объект {index}.</b> {esc(noun.capitalize())}, кадастровый номер <code>{esc(obj.cad_number)}</code>"
        head += f", {located} по адресу: {esc(obj.address)}." if obj.address else "."
        blocks.append("\n".join([head, *_offer_characteristics(obj)]))

    total_cost = sum(obj.cost_value for obj in objects if obj.cost_value is not None)
    has_cost = [obj for obj in objects if obj.cost_value is not None]
    summary = ["<b>Итого по залогу</b>", f"• Количество объектов: {len(objects)}"]
    if has_cost:
        note = "" if len(has_cost) == len(objects) else f" (по {len(has_cost)} из {len(objects)} объектов)"
        summary.append(f"• Суммарная кадастровая стоимость: {esc(_money(total_cost))}{note}")
    blocks.append("\n".join(summary))

    has_premises = any(obj.kind is ObjectKind.PREMISES for obj in objects)
    blocks.append("\n".join([
        *_loan_terms(answers),
        "",
        *_additional(
            answers, has_premises=has_premises, documents="• Документы и фотографии предметов залога прилагаются."
        ),
    ]))
    return split_message(blocks)


def split_message(blocks: list[str], limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Склеивает блоки через пустую строку в сообщения не длиннее limit."""
    messages: list[str] = []
    current = ""
    for block in blocks:
        block = fit_message(block, limit)
        candidate = f"{current}\n\n{block}" if current else block
        if len(candidate) <= limit:
            current = candidate
        else:
            messages.append(current)
            current = block
    if current:
        messages.append(current)
    return messages


# ------------------------------------------------------------------------------
# Пакетный запрос
# ------------------------------------------------------------------------------


def format_batch_line(index: int, kad_number: str, obj: NspdObject | None, error: str | None) -> str:
    head = f"{index}. <code>{esc(kad_number)}</code>"
    if obj is None:
        return f"{head} — {esc(error or 'нет данных')}"
    parts = _compact([
        esc(obj.type_label),
        esc(_area(obj)),
        esc(_money(obj.cost_value)),
    ])
    line = f"{head} — {', '.join(parts)}"
    if obj.address:
        line += f"\n    {esc(truncate(obj.address, 160))}"
    return line


CSV_COLUMNS: list[tuple[str, Any]] = [
    ("Кадастровый номер", lambda o: o.cad_number),
    ("Тип", lambda o: o.type_label),
    ("Адрес", lambda o: o.address),
    ("Площадь, кв. м", lambda o: fmt_number(o.area)),
    ("Назначение", lambda o: o.purpose),
    ("Категория земель", lambda o: o.land_category),
    ("ВРИ", lambda o: o.permitted_use),
    ("Этаж", lambda o: o.floor),
    ("Этажность", lambda o: o.floors),
    ("Материал стен", lambda o: o.materials),
    ("Год постройки", lambda o: o.year_built or o.year_commissioning),
    ("Статус", lambda o: o.status),
    ("Форма собственности", lambda o: o.ownership_type),
    ("Вид права", lambda o: o.right_type),
    ("Кадастровая стоимость, руб.", lambda o: fmt_number(o.cost_value)),
    ("Удельный показатель, руб./кв. м", lambda o: fmt_number(o.cost_index)),
    ("Дата определения стоимости", lambda o: fmt_date(o.cost_determination_date)),
    ("Кадастровый квартал", lambda o: o.quarter),
    ("Здание", lambda o: o.parent_cad_number),
]


def build_csv(rows: list[tuple[str, NspdObject | None, str | None]]) -> bytes:
    """CSV в кодировке UTF-8 с BOM и разделителем ';' — открывается в Excel без настроек."""
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow([title for title, _ in CSV_COLUMNS] + ["Ошибка"])
    for kad_number, obj, error in rows:
        if obj is None:
            writer.writerow([kad_number] + [""] * (len(CSV_COLUMNS) - 1) + [error or ""])
        else:
            writer.writerow([_csv_value(getter(obj)) for _, getter in CSV_COLUMNS] + [""])
    return buffer.getvalue().encode("utf-8-sig")


def _csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    return str(value)
