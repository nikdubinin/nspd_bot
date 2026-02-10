import asyncio
import html
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Final

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from nspd_request import NSPD  # неофициальная обёртка над НСПД[nspd][web:12]

# ------------------------------------------------------------------------------
# Конфигурация логирования
# ------------------------------------------------------------------------------

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger: Final = logging.getLogger(__name__)

# ------------------------------------------------------------------------------
# Загрузка конфигурации из .env
# ------------------------------------------------------------------------------

load_dotenv()

TELEGRAM_BOT_TOKEN: Final[str | None] = os.getenv("TELEGRAM_BOT_TOKEN")

if not TELEGRAM_BOT_TOKEN:
    raise RuntimeError(
        "Не задан TELEGRAM_BOT_TOKEN. Создайте .env на основе config.example.env"
    )

# ------------------------------------------------------------------------------
# Инициализация клиента НСПД
# ------------------------------------------------------------------------------

# NSPD использует открытый API НСПД и не требует ключа.[web:12]
nspd_api: Final = NSPD()

# ------------------------------------------------------------------------------
# Кэш ответов НСПД (минимизация задержек и нагрузки)
# ------------------------------------------------------------------------------

_CACHE_TTL_SECONDS: Final[int] = 30 * 60  # 30 минут
_nspd_cache: dict[str, tuple[float, dict]] = {}

# ------------------------------------------------------------------------------
# Вспомогательные функции
# ------------------------------------------------------------------------------

def _esc(value: object) -> str:
    """HTML-escape для безопасного вывода в Telegram (parse_mode=HTML)."""
    if value is None:
        return ""
    return html.escape(str(value))


def _truncate(text: object, max_len: int = 600) -> str:
    if text is None:
        return ""
    if isinstance(text, (list, tuple, set)):
        s = "; ".join(str(x) for x in text if x is not None)
    else:
        s = str(text)
    s = s.strip()
    if len(s) <= max_len:
        return s
    return s[: max_len - 3].rstrip() + "..."


def _fmt_date_iso(date_str: object) -> str:
    """YYYY-MM-DD -> DD.MM.YYYY (если распознали)."""
    if not date_str:
        return ""
    s = str(date_str).strip()
    try:
        dt = datetime.fromisoformat(s)
        return dt.strftime("%d.%m.%Y")
    except ValueError:
        pass
    try:
        dt = datetime.strptime(s, "%Y-%m-%d")
        return dt.strftime("%d.%m.%Y")
    except ValueError:
        return s


def _fmt_dt_iso(dt_str: object) -> str:
    """YYYY-MM-DDTHH:MM:SS -> DD.MM.YYYY HH:MM (если распознали)."""
    if not dt_str:
        return ""
    s = str(dt_str).strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.strftime("%d.%m.%Y %H:%M")
    except ValueError:
        return s


def _fmt_number(value: object) -> str:
    """Числа -> '1 234,56' (если возможно)."""
    if value is None or value == "":
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    # оставим до 2 знаков, уберём .00
    s = f"{num:,.2f}".replace(",", " ").replace(".", ",")
    if s.endswith(",00"):
        s = s[:-3]
    return s


def _line_kv(label: str, value: object, *, code: bool = False) -> str | None:
    if value is None or value == "":
        return None
    v = _esc(value)
    if code:
        v = f"<code>{v}</code>"
    return f"• <b>{_esc(label)}:</b> {v}"


def _classify_object(obj_type: str | None, category_name: str | None) -> tuple[str, str]:
    """
    Черновая классификация объекта для текстов инвесторам.

    Возвращает (человекочитаемый тип, причастие 'расположенный/расположенная').
    """
    t = (obj_type or category_name or "").lower()

    if "квартира" in t:
        return "квартира", "расположенная"
    if "комната" in t:
        return "комната", "расположенная"
    if "жил" in t and "дом" in t:
        return "жилой дом", "расположенный"
    if "здание" in t:
        return "здание", "расположенное"
    if "сооружение" in t:
        return "сооружение", "расположенное"
    if "зем" in t or t in {"зу", "з/у"}:
        return "земельный участок", "расположенный"

    return "объект недвижимости", "расположенный"


def _build_investor_message(data: dict, *, kind: str) -> str:
    """
    Строит текст для инвесторов по данным НСПД.

    kind: 'loan' (займ под залог) или 'buyout' (срочный выкуп).
    """
    kad = data.get("kad_number") or ""
    obj_type = data.get("object_type") or data.get("type")

    options: dict = {}
    nspd_data = data.get("data")
    category_name = ""
    if isinstance(nspd_data, dict):
        feats = nspd_data.get("features") or []
        if feats:
            props = feats[0].get("properties", {}) or {}
            category_name = props.get("categoryName") or ""
            options = props.get("options", {}) or {}

    address = options.get("readable_address") or ""
    area = options.get("build_record_area") or options.get("specified_area") or options.get("area")
    purpose = options.get("purpose") or options.get("permitted_use_name")
    status = options.get("status")
    ownership_type = options.get("ownership_type")
    floors = options.get("floors")
    materials = options.get("materials")
    year_built = options.get("year_built")
    quarter_cad_number = options.get("quarter_cad_number")
    cost_value = options.get("cost_value")

    human_type, located_word = _classify_object(
        obj_type=obj_type,
        category_name=category_name,
    )

    lines: list[str] = []

    # Приветствие и вводная
    if kind == "loan":
        lines.append("<b>Предложение по займу под залог</b>\n")
    else:
        lines.append("<b>Предложение на срочный выкуп</b>\n")

    lines.append("Добрый день, уважаемые коллеги!\n")

    main_line = (
        f"Вашему вниманию к рассмотрению предлагается {html.escape(human_type)}, "
        f"с кадастровым номером <code>{_esc(kad)}</code>, "
    )
    if address:
        main_line += f"{_esc(located_word)} по адресу: {_esc(address)}."
    else:
        main_line = main_line.rstrip(", ") + "."
    lines.append(main_line + "\n")

    # Характеристики
    lines.append("<b>Краткие характеристики объекта</b>")

    char_lines: list[str] = []
    if area not in (None, ""):
        char_lines.append(f"• Площадь: {_esc(_fmt_number(area))} кв. м")
    if purpose:
        char_lines.append(f"• Назначение/вид использования: {_esc(purpose)}")
    if status:
        char_lines.append(f"• Статус по НСПД: {_esc(status)}")
    if ownership_type:
        char_lines.append(f"• Форма собственности: {_esc(ownership_type)}")
    if floors:
        char_lines.append(f"• Количество этажей: {_esc(floors)}")
    if materials:
        char_lines.append(f"• Материал стен/конструкций: {_esc(materials)}")
    if year_built:
        char_lines.append(f"• Год завершения строительства: {_esc(year_built)}")
    if quarter_cad_number:
        char_lines.append(
            f"• Кадастровый квартал: <code>{_esc(quarter_cad_number)}</code>"
        )
    if cost_value not in (None, ""):
        char_lines.append(
            f"• Кадастровая стоимость (ориентир): {_esc(_fmt_number(cost_value))} руб."
        )

    if char_lines:
        lines.extend(char_lines)
    else:
        lines.append("• Характеристики в открытом НСПД недоступны или не заполнены.")

    # Блок условий / пожеланий
    if kind == "loan":
        lines.append("\n<b>Параметры предполагаемого займа</b>")
        lines.append("• Требуемая сумма займа: __________________")
        lines.append("• Срок займа: __________________")
        lines.append("• Цель займа: __________________")

        lines.append("\n<b>Информация о заемщике</b>")
        lines.append("• Место работы / должность: __________________")
        lines.append("• Ежемесячный совокупный доход: __________________")
    else:
        lines.append("\n<b>Параметры сделки по выкупу</b>")
        lines.append("• Ожидаемая цена срочного выкупа: __________________")
        lines.append("• Желаемый срок завершения сделки: __________________")

    # Обременения и документы
    lines.append("\n<b>Дополнительно</b>")
    lines.append("• Наличие обременений: __________________")
    lines.append(
        "• Документы и фотографии предмета залога прилагаются к данному сообщению."
    )

    lines.append("\nЖдём ваших предложений и вопросов.")

    return "\n".join(lines)


async def _fetch_nspd_object(kad_number: str) -> dict:
    """
    Получение сведений об объекте из NSPD с кэшем и небольшим числом повторов.

    Возвращает словарь, совместимый с NSPD-request get_object_info.
    """
    now = time.time()

    # Кэш по кадастровому номеру
    cached = _nspd_cache.get(kad_number)
    if cached is not None:
        ts, data = cached
        if now - ts <= _CACHE_TTL_SECONDS:
            logger.info("Используем кэш НСПД для %s", kad_number)
            return data

    last_data: dict | None = None

    for attempt in range(3):
        data: dict = await asyncio.to_thread(
            nspd_api.get_object_info,
            kad_number,
            include_geom_id=True,
            include_object_type=True,
            include_related=True,
        )
        last_data = data

        # Если нет ошибки — кэшируем и выходим
        if not isinstance(data, dict) or not data.get("error"):
            _nspd_cache[kad_number] = (now, data)
            return data

        # Ошибка — имеет ли смысл повторять?
        err_text = str(data.get("error", "")).lower()
        is_timeout = "timeout" in err_text or "time out" in err_text
        is_connect = "connection" in err_text or "connect" in err_text

        if attempt < 2 and (is_timeout or is_connect):
            # Небольшая задержка перед повтором
            await asyncio.sleep(2 * (attempt + 1))
            continue

        # Прочие ошибки/последняя попытка — выходим
        break

    return last_data or {}


def normalize_kad_number(raw: str) -> str | None:
    """
    Приводит кадастровый номер к виду ХХ:ХХ:XXXXXXX:XXX.

    Допускает ввод с пробелами, без двоеточий, с лишними символами.
    Возвращает None, если строку нельзя привести к валидному формату.
    """
    # Оставляем только цифры и двоеточия
    cleaned = re.sub(r"[^0-9:]", "", raw)

    # Если пользователь ввёл только цифры — пытаемся вставить двоеточия по шаблону
    if ":" not in cleaned and len(cleaned) >= 11:
        # Простейшая попытка: 2-2-7-...
        # В реальном проде лучше использовать более строгую валидацию
        cleaned = f"{cleaned[0:2]}:{cleaned[2:4]}:{cleaned[4:-3]}:{cleaned[-3:]}"

    pattern = re.compile(r"^\d{2}:\d{2}:\d{1,7}:\d{1,7}$")
    if not pattern.match(cleaned):
        return None

    return cleaned


def format_object_info(data: dict, *, short: bool = False) -> str:
    """
    Форматирует ответ НСПД в человекочитаемое сообщение Telegram.

    Ожидается, что data — это результат nspd_api.get_object_info(...).[web:12]
    Структура словаря описана в документации NSPD-request:
    https://github.com/Logar1t/NSPD-request[web:10]
    """
    # ------------------------------------------------------------------
    # Извлечение базовых полей из структуры NSPD-request
    # ------------------------------------------------------------------

    kad = data.get("kad_number")
    obj_type = data.get("object_type") or data.get("type")
    geom_id = data.get("geom_id")

    options: dict = {}
    system_info: dict = {}
    category_name = ""
    nspd_data = data.get("data")
    if isinstance(nspd_data, dict):
        features = nspd_data.get("features") or []
        if features:
            properties = features[0].get("properties", {}) or {}
            system_info = properties.get("systemInfo", {}) or {}
            category_name = properties.get("categoryName") or ""
            options = properties.get("options", {}) or {}

    # Поля характеристик объекта (для ЗУ/зданий и т.п.)
    name = options.get("building_name") or options.get("name")
    address = options.get("readable_address")
    area = (
        options.get("build_record_area")
        or options.get("specified_area")
        or options.get("area")
    )
    purpose = options.get("purpose") or options.get("permitted_use_name")
    purpose_doc = options.get("permitted_use_established_by_document")
    status = options.get("status")
    ownership_type = options.get("ownership_type")
    floors = options.get("floors")
    underground_floors = options.get("underground_floors")
    materials = options.get("materials")
    year_built = options.get("year_built")
    year_commissioning = options.get("year_commisioning")

    # Стоимость
    cost_value = options.get("cost_value")
    cost_index = options.get("cost_index")
    cost_determination_date = options.get("cost_determination_date")
    cost_registration_date = options.get("cost_registration_date")

    # Прочие идентификаторы
    quarter_cad_number = options.get("quarter_cad_number")
    facility_cad_number = options.get("facility_cad_number")
    united_cad_numbers = options.get("united_cad_numbers")
    intersected_cad_numbers = options.get("intersected_cad_numbers")

    inserted_at = system_info.get("inserted")
    updated_at = system_info.get("updated")

    # ------------------------------------------------------------------
    # Краткий формат (для режима /short)
    # ------------------------------------------------------------------
    if short:
        lines: list[str] = ["<b>Краткие сведения об объекте</b>"]
        if kad:
            lines.append(f"<b>Кадастровый номер:</b> <code>{_esc(kad)}</code>")
        if obj_type or category_name:
            lines.append(f"<b>Тип:</b> {_esc(obj_type or category_name)}")
        if address:
            lines.append(f"<b>Адрес:</b> <b>{_esc(address)}</b>")
        if area is not None and area != "":
            lines.append(f"<b>Площадь:</b> {_esc(_fmt_number(area))} кв. м")
        if purpose:
            lines.append(f"<b>Назначение:</b> {_esc(purpose)}")
        if cost_value is not None:
            lines.append(
                f"<b>Кадастровая стоимость:</b> {_esc(_fmt_number(cost_value))} руб."
            )
        if updated_at or inserted_at:
            when = _fmt_dt_iso(updated_at or inserted_at)
            lines.append(f"<b>Актуализация в НСПД:</b> {_esc(when)}")
        lines.append(
            "\n<i>Отправьте команду /full, чтобы увидеть расширенные сведения.</i>"
        )
        return "\n".join(lines)

    lines: list[str] = []

    # ------------------------------------------------------------------
    # Заголовок
    # ------------------------------------------------------------------
    lines.append("<b>Сведения об объекте</b>")
    if kad:
        lines.append(f"<b>Кадастровый номер:</b> <code>{_esc(kad)}</code>")
    if obj_type or category_name:
        lines.append(f"<b>Тип:</b> {_esc(obj_type or category_name)}")
    if name:
        lines.append(f"<b>Наименование:</b> {_esc(name)}")
    if address:
        lines.append(f"<b>Адрес:</b> <b>{_esc(address)}</b>")
    if geom_id:
        lines.append(f"<b>geom_id:</b> <code>{_esc(geom_id)}</code>")
    if updated_at or inserted_at:
        when = _fmt_dt_iso(updated_at or inserted_at)
        lines.append(f"<b>Актуализация в НСПД:</b> {_esc(when)}")

    related = data.get("related") or []
    if related:
        lines.append("\n<b>Связанные объекты</b>")
        if isinstance(related, list):
            shown = related[:10]
            for cad_rel in shown:
                lines.append(f"• <code>{_esc(cad_rel)}</code>")
            if len(related) > len(shown):
                lines.append(f"• ...и ещё {len(related) - len(shown)}")
        else:
            lines.append(f"• {_esc(_truncate(related, 500))}")

    # ------------------------------------------------------------------
    # Характеристики объекта
    # ------------------------------------------------------------------
    lines.append("\n<b>Характеристики</b>")

    kv_lines: list[str] = []
    if area is not None and area != "":
        kv_lines.append(f"• <b>Площадь:</b> {_esc(_fmt_number(area))} кв. м")
    if purpose:
        kv_lines.append(f"• <b>Назначение:</b> {_esc(purpose)}")
    if status:
        kv_lines.append(f"• <b>Статус:</b> {_esc(status)}")
    if ownership_type:
        kv_lines.append(f"• <b>Форма собственности:</b> {_esc(ownership_type)}")
    if floors:
        kv_lines.append(f"• <b>Этажей:</b> {_esc(floors)}")
    if underground_floors:
        kv_lines.append(f"• <b>Подземных этажей:</b> {_esc(underground_floors)}")
    if materials:
        kv_lines.append(f"• <b>Материал стен:</b> {_esc(materials)}")
    if year_built:
        kv_lines.append(f"• <b>Год завершения строительства:</b> {_esc(year_built)}")
    if year_commissioning:
        kv_lines.append(f"• <b>Год ввода в эксплуатацию:</b> {_esc(year_commissioning)}")
    if quarter_cad_number:
        kv_lines.append(f"• <b>Кадастровый квартал:</b> <code>{_esc(quarter_cad_number)}</code>")
    if facility_cad_number:
        kv_lines.append(
            f"• <b>Условный/связанный номер:</b> <code>{_esc(facility_cad_number)}</code>"
        )
    if kv_lines:
        lines.extend(kv_lines)
    else:
        lines.append("• (нет дополнительных характеристик в открытых данных НСПД)")

    if purpose_doc:
        lines.append("\n<b>Разрешённое использование (по документу)</b>")
        lines.append(f"• {_esc(_truncate(purpose_doc, 800))}")

    if united_cad_numbers:
        lines.append("\n<b>Единые/объединённые номера</b>")
        lines.append(f"• {_esc(_truncate(united_cad_numbers, 500))}")
    if intersected_cad_numbers:
        lines.append("\n<b>Пересечения</b>")
        lines.append(f"• {_esc(_truncate(intersected_cad_numbers, 500))}")

    # ------------------------------------------------------------------
    # Сведения о кадастровой стоимости
    # ------------------------------------------------------------------
    if cost_value is not None:
        lines.append("\n<b>Кадастровая стоимость</b>")
        lines.append(f"• <b>Стоимость:</b> {_esc(_fmt_number(cost_value))} руб.")
        if cost_index is not None:
            lines.append(
                f"• <b>Удельный показатель:</b> {_esc(_fmt_number(cost_index))} руб./кв. м"
            )
        if cost_determination_date:
            lines.append(
                f"• <b>Дата определения:</b> {_esc(_fmt_date_iso(cost_determination_date))}"
            )
        if cost_registration_date:
            lines.append(
                f"• <b>Дата внесения:</b> {_esc(_fmt_date_iso(cost_registration_date))}"
            )

    # ------------------------------------------------------------------
    # Права и обременения
    # ------------------------------------------------------------------
    lines.append("\n<b>Права и обременения</b>")
    lines.append(
        "• Открытый НСПД-геопортал (и библиотека NSPD-request) не отдают "
        "детализированные сведения о зарегистрированных правах и обременениях.\n"
        "• Для официальных данных по правам/ограничениям нужна выписка ЕГРН."
    )

    return "\n".join(lines)


# ------------------------------------------------------------------------------
# Обработчики Telegram
# ------------------------------------------------------------------------------


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает команду /start.

    Приветствует пользователя и объясняет, что нужно отправить
    кадастровый номер объекта недвижимости.
    """
    user_name = update.effective_user.first_name or "коллега"
    text = (
        f"Здравствуйте, {user_name}!\n\n"
        "Я бот для получения сведений об объектах недвижимости по кадастровому номеру.\n"
        "Пожалуйста, отправьте мне кадастровый номер (например: 77:03:0002007:7190), "
        "и я постараюсь вывести характеристики объекта, сведения о правах и ограничениях."
    )
    await update.effective_message.reply_text(text)


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Обрабатывает команду /help.

    Краткая инструкция по использованию бота.
    """
    text = (
        "<b>Как пользоваться ботом</b>\n\n"
        "Отправьте мне кадастровый номер, а я запрошу данные в НСПД и пришлю ответ.\n\n"
        "<b>Примеры ввода</b>\n"
        "• <code>77:03:0002007:7190</code>\n"
        "• <code>77030002007 7190</code>\n"
        "• <code>77 03 0002007 7190</code>\n\n"
        "<b>Команды</b>\n"
        "• /start — начать\n"
        "• /help — справка\n"
        "• /short — краткий формат ответа\n"
        "• /full — подробный формат ответа\n\n"
        "<b>Кнопки под ответом</b>\n"
        "• <i>Открыть в НСПД</i> — откроет объект на карте НСПД\n"
        "• <i>Показать связанные объекты</i> — список связанных кадастровых номеров (если есть)\n"
        "• <i>Показать raw JSON</i> — сырые данные НСПД (для отладки)\n\n"
        "<b>Важно</b>\n"
        "Открытый НСПД-геопортал не отдаёт детализированные сведения о зарегистрированных "
        "правах и обременениях. Для официальных данных нужна выписка ЕГРН."
    )
    await update.effective_message.reply_text(text, parse_mode="HTML")


async def set_mode_short_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Включает краткий формат вывода сведений."""
    context.chat_data["view_mode"] = "short"
    await update.effective_message.reply_text(
        "Теперь бот будет показывать <b>краткие</b> сведения об объекте.\n"
        "Чтобы вернуть подробный формат, используйте команду /full.",
        parse_mode="HTML",
    )


async def set_mode_full_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Включает подробный формат вывода сведений."""
    context.chat_data["view_mode"] = "full"
    await update.effective_message.reply_text(
        "Теперь бот будет показывать <b>подробные</b> сведения об объекте.",
        parse_mode="HTML",
    )


async def kad_number_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """
    Обрабатывает произвольные текстовые сообщения как кадастровый номер.

    1. Пытается нормализовать введённый текст в кадастровый номер.
    2. Если формат некорректен — просит пользователя проверить номер.
    3. Если формат в порядке — запрашивает данные из НСПД через nspd_request.[web:12]
    4. Отправляет пользователю отформатированный результат или сообщение об ошибке.
    """
    message = update.effective_message
    raw_text = (message.text or "").strip()

    # Игнорируем команды (они обрабатываются отдельно)
    if raw_text.startswith("/"):
        return

    kad_number = normalize_kad_number(raw_text)

    if not kad_number:
        await message.reply_text(
            "Похоже, это не очень похоже на кадастровый номер.\n\n"
            "Пожалуйста, пришлите номер в формате вида:\n"
            "<code>77:03:0002007:7190</code>\n"
            "или просто цифрами, я попробую распознать его автоматически.",
            parse_mode="HTML",
        )
        return

    try:
        await message.reply_text(
            f"Принял кадастровый номер: <code>{kad_number}</code>\n"
            "Запрашиваю данные в Национальной системе пространственных данных...",
            parse_mode="HTML",
        )

        # Универсальная функция возвращает словарь с данными объекта.[web:12]
        # Получаем данные через обёртку с кэшем и повторами.
        data: dict = await _fetch_nspd_object(kad_number)

        if not data:
            await message.reply_text(
                "К сожалению, по этому кадастровому номеру "
                "не удалось получить данные из НСПД.\n"
                "Попробуйте, пожалуйста, проверить номер или отправить другой."
            )
            return

        # Проверка на наличие сообщений об ошибке в ответе
        if isinstance(data, dict) and data.get("error"):
            await message.reply_text(
                f"НСПД вернула ошибку: {data['error']}\n\n"
                "Попробуйте позднее или проверьте корректность кадастрового номера."
            )
            return

        view_mode = context.chat_data.get("view_mode", "full")
        formatted = format_object_info(data, short=(view_mode == "short"))

        # Кнопки под сообщением: открыть в НСПД, показать связанные объекты, raw JSON,
        # а также сформировать текстовые предложения для инвесторов.
        buttons: list[list[InlineKeyboardButton]] = []

        # URL НСПД с подставленным кадастровым номером (простой вариант).
        if kad_number:
            nspd_url = (
                "https://nspd.gov.ru/map?thematic=PKK"
                f"&query={kad_number}"
            )
            buttons.append(
                [InlineKeyboardButton("Открыть в НСПД", url=nspd_url)]
            )

        if kad_number:
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Показать связанные объекты",
                        callback_data=f"rel:{kad_number}",
                    )
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Показать raw JSON",
                        callback_data=f"raw:{kad_number}",
                    )
                ]
            )

            # Кнопки для генерации текстов-предложений
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Сформировать предложение по займу под залог",
                        callback_data=f"loan:{kad_number}",
                    )
                ]
            )
            buttons.append(
                [
                    InlineKeyboardButton(
                        "Сформировать предложение на срочный выкуп",
                        callback_data=f"buy:{kad_number}",
                    )
                ]
            )

        reply_markup = InlineKeyboardMarkup(buttons) if buttons else None
        await message.reply_text(formatted, parse_mode="HTML", reply_markup=reply_markup)

    except Exception as exc:
        logger.exception("Ошибка при обращении к НСПД: %s", exc)
        await message.reply_text(
            "Произошла внутренняя ошибка при запросе данных в НСПД.\n"
            "Пожалуйста, попробуйте ещё раз чуть позже."
        )


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Глобальный обработчик ошибок Telegram‑бота.

    Логирует исключения, чтобы не падало всё приложение.
    """
    logger.error("Произошла ошибка при обработке обновления: %s", context.error)


async def callback_query_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Обрабатывает нажатия на inline-кнопки под сообщением бота."""
    query = update.callback_query
    if not query:
        return

    data = query.data or ""
    await query.answer()

    if data.startswith("rel:"):
        kad_number = data[4:]
        obj = await _fetch_nspd_object(kad_number)
        if not obj or (isinstance(obj, dict) and obj.get("error")):
            await query.message.reply_text(
                "Не удалось получить сведения о связанных объектах.\n"
                "Попробуйте ещё раз позднее.",
                parse_mode="HTML",
            )
            return

        related = obj.get("related") or []
        if not related:
            await query.message.reply_text(
                "Связанные объекты не найдены в открытых данных НСПД.",
                parse_mode="HTML",
            )
            return

        text_lines = [
            f"<b>Связанные объекты для {html.escape(kad_number)}</b>",
        ]
        if isinstance(related, list):
            shown = related[:50]
            for cad_rel in shown:
                text_lines.append(f"• <code>{_esc(cad_rel)}</code>")
            if len(related) > len(shown):
                text_lines.append(
                    f"• ...и ещё {len(related) - len(shown)} (не показаны для краткости)"
                )
        else:
            text_lines.append(f"• {_esc(_truncate(related, 1500))}")

        await query.message.reply_text(
            "\n".join(text_lines),
            parse_mode="HTML",
        )
        return

    if data.startswith("raw:"):
        kad_number = data[4:]
        obj = await _fetch_nspd_object(kad_number)
        if not obj or (isinstance(obj, dict) and obj.get("error")):
            await query.message.reply_text(
                "Не удалось получить сырые данные НСПД для этого объекта.",
                parse_mode="HTML",
            )
            return

        try:
            raw = json.dumps(obj, ensure_ascii=False, indent=2)
        except TypeError:
            raw = str(obj)

        raw_escaped = html.escape(_truncate(raw, 3500))
        text = (
            f"<b>Сырые данные НСПД для {html.escape(kad_number)}</b>\n"
            f"<pre>{raw_escaped}</pre>"
        )
        await query.message.reply_text(text, parse_mode="HTML")

        return

    if data.startswith("loan:"):
        kad_number = data[5:]
        obj = await _fetch_nspd_object(kad_number)
        if not obj or (isinstance(obj, dict) and obj.get("error")):
            await query.message.reply_text(
                "Не удалось сформировать предложение по займу под залог — "
                "НСПД не вернула корректные данные по объекту.",
                parse_mode="HTML",
            )
            return

        text = _build_investor_message(obj, kind="loan")
        await query.message.reply_text(text, parse_mode="HTML")
        return

    if data.startswith("buy:"):
        kad_number = data[4:]
        obj = await _fetch_nspd_object(kad_number)
        if not obj or (isinstance(obj, dict) and obj.get("error")):
            await query.message.reply_text(
                "Не удалось сформировать предложение на срочный выкуп — "
                "НСПД не вернула корректные данные по объекту.",
                parse_mode="HTML",
            )
            return

        text = _build_investor_message(obj, kind="buyout")
        await query.message.reply_text(text, parse_mode="HTML")


# ------------------------------------------------------------------------------
# Точка входа
# ------------------------------------------------------------------------------


def main() -> None:
    """
    Точка входа в приложение.

    Создаёт и запускает асинхронное приложение python-telegram-bot.
    """
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Команды
    application.add_handler(CommandHandler("start", start_handler))
    application.add_handler(CommandHandler("help", help_handler))
    application.add_handler(CommandHandler("short", set_mode_short_handler))
    application.add_handler(CommandHandler("full", set_mode_full_handler))

    # Любой текст, не начинающийся с /, считаем попыткой ввести кадастровый номер
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, kad_number_handler)
    )

    # Колбэки от inline-кнопок
    application.add_handler(CallbackQueryHandler(callback_query_handler))

    # Глобальный обработчик ошибок
    application.add_error_handler(error_handler)

    logger.info("Бот запущен. Ожидание сообщений...")

    # В Python 3.12+ по умолчанию нет "текущего" event loop в main‑потоке,
    # а python-telegram-bot 21.6 ожидает, что он уже создан.
    # Явно создаём и назначаем цикл, чтобы избежать RuntimeError.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    application.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
