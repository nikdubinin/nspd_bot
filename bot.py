# bot.py
import asyncio
import logging
import os
import re
from typing import Final

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
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
# Вспомогательные функции
# ------------------------------------------------------------------------------


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


def format_object_info(data: dict) -> str:
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
    nspd_data = data.get("data")
    if isinstance(nspd_data, dict):
        features = nspd_data.get("features") or []
        if features:
            properties = features[0].get("properties", {}) or {}
            options = properties.get("options", {}) or {}

    # Поля характеристик объекта (для ЗУ/зданий и т.п.)
    address = options.get("readable_address")
    area = (
        options.get("build_record_area")
        or options.get("specified_area")
        or options.get("area")
    )
    purpose = options.get("purpose") or options.get("permitted_use_name")
    status = options.get("status")
    ownership_type = options.get("ownership_type")
    floors = options.get("floors")
    underground_floors = options.get("underground_floors")
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

    lines: list[str] = []

    lines.append("Информация по объекту:\n")

    if kad:
        lines.append(f"• Кадастровый номер: <code>{kad}</code>")
    if obj_type:
        lines.append(f"• Тип объекта: {obj_type}")
    if geom_id:
        lines.append(f"• Идентификатор геометрии (geom_id): <code>{geom_id}</code>")

    # ------------------------------------------------------------------
    # Характеристики объекта
    # ------------------------------------------------------------------
    lines.append("\nХарактеристики объекта:")
    if address:
        lines.append(f"• Адрес: {address}")
    if area is not None:
        lines.append(f"• Площадь: {area} кв. м")
    if purpose:
        lines.append(f"• Назначение: {purpose}")
    if status:
        lines.append(f"• Статус: {status}")
    if ownership_type:
        lines.append(f"• Форма собственности: {ownership_type}")
    if floors:
        lines.append(f"• Количество этажей: {floors}")
    if underground_floors:
        lines.append(f"• Количество подземных этажей: {underground_floors}")
    if year_built:
        lines.append(f"• Год завершения строительства: {year_built}")
    if year_commissioning:
        lines.append(f"• Год ввода в эксплуатацию: {year_commissioning}")

    if quarter_cad_number or facility_cad_number:
        lines.append("\nРанее присвоенные/связанные номера:")
        if quarter_cad_number:
            lines.append(f"• Кадастровый квартал: <code>{quarter_cad_number}</code>")
        if facility_cad_number:
            lines.append(
                f"• Условный номер / связанный объект: <code>{facility_cad_number}</code>"
            )

    # ------------------------------------------------------------------
    # Сведения о кадастровой стоимости
    # ------------------------------------------------------------------
    if cost_value is not None:
        lines.append("\nСведения о кадастровой стоимости:")
        lines.append(f"• Кадастровая стоимость: {cost_value} руб.")
        if cost_index is not None:
            lines.append(f"• Удельный показатель: {cost_index} руб./кв. м")
        if cost_determination_date:
            lines.append(f"• Дата определения стоимости: {cost_determination_date}")
        if cost_registration_date:
            lines.append(f"• Дата внесения стоимости: {cost_registration_date}")

    # ------------------------------------------------------------------
    # Права и обременения
    # ------------------------------------------------------------------
    lines.append("\nПрава и обременения:")
    lines.append(
        "• Открытый сервис НСПД (через библиотеку NSPD-request) не "
        "предоставляет детализированные сведения о зарегистрированных правах "
        "и обременениях. Для получения таких данных требуется выписка ЕГРН."
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
        "Инструкция по использованию бота:\n\n"
        "1. Отправьте мне кадастровый номер объекта недвижимости.\n"
        "2. Я обращусь к НСПД и верну доступные сведения об объекте.\n\n"
        "Примеры допустимых форматов:\n"
        "• 77:03:0002007:7190\n"
        "• 77030002007 7190\n"
        "• 77 03 0002007 7190\n\n"
        "Если номер будет некорректен, я вежливо попрошу вас его уточнить."
    )
    await update.effective_message.reply_text(text)


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

    await message.reply_text(
        f"Принял кадастровый номер: <code>{kad_number}</code>\n"
        "Запрашиваю данные в Национальной системе пространственных данных...",
        parse_mode="HTML",
    )

    try:
        # Универсальная функция возвращает словарь с данными объекта.[web:12]
        # В актуальной версии nspd-request метод называется get_object_info.
        data: dict = nspd_api.get_object_info(
            kad_number,
            include_geom_id=True,
            include_object_type=True,
        )

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

        formatted = format_object_info(data)
        await message.reply_text(formatted, parse_mode="HTML")

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

    # Любой текст, не начинающийся с /, считаем попыткой ввести кадастровый номер
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, kad_number_handler)
    )

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
