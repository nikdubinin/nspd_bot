"""Обработчики команд, сообщений и inline-кнопок Telegram."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Message, Update
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop, ContextTypes

from .activity import Activity
from .cadastral import extract_kad_numbers
from .config import Settings
from .formatting import (
    build_csv,
    esc,
    fit_message,
    format_batch_line,
    format_card,
    format_offer,
    format_pledge_offer,
    format_related,
    nspd_url,
    yandex_maps_url,
)
from .models import NspdObject, ObjectKind
from .offer_form import OfferForm, OfferKind
from .nspd_service import NspdNotFound, NspdService, NspdUnavailable

logger = logging.getLogger(__name__)

HISTORY_SIZE = 15
PLEDGE_KEY = "pledge_pending"
FORM_KEY = "offer_form"
EXAMPLE_NUMBER = "77:01:0001001:1000"

HELP_TEXT = (
    "<b>Как пользоваться ботом</b>\n\n"
    "Отправьте кадастровый номер — бот запросит сведения в НСПД и пришлёт карточку объекта.\n"
    "Можно прислать <b>сразу несколько номеров</b> (каждый с новой строки или через запятую) — "
    "бот вернёт сводку и таблицу CSV для Excel.\n\n"
    "<b>Форматы ввода</b>\n"
    "• <code>77:03:0002007:7190</code>\n"
    "• <code>77 03 0002007 7190</code>\n"
    "• <code>77030002007 7190</code>\n\n"
    "<b>Команды</b>\n"
    "/short — краткие карточки\n"
    "/full — подробные карточки\n"
    "/history — последние запросы\n"
    "/pledge — займ под залог нескольких объектов\n"
    "/cancel — отменить ввод номеров или анкету\n"
    "/help — эта справка\n\n"
    "<b>Кнопки под карточкой</b>\n"
    "• НСПД / Яндекс.Карты — объект на карте\n"
    "• Связанные объекты — здания на участке, участки под зданием, здание для помещения\n"
    "• Займ под залог — бот спросит, один объект в залоге или несколько, задаст вопросы "
    "о займе и заёмщике и соберёт готовый текст для инвесторов\n"
    "• Срочный выкуп — бот спросит цену, сроки и обременения и соберёт текст\n"
    "На любой вопрос можно ответить текстом, выбрать вариант или пропустить — "
    "тогда в тексте останется пустая графа.\n"
    "• Обновить — запросить свежие данные, минуя кэш\n"
    "• JSON — исходный ответ НСПД файлом\n\n"
    "<i>Сведения о правообладателях и обременениях НСПД в открытом доступе не публикует — "
    "для них нужна выписка из ЕГРН.</i>"
)


# ------------------------------------------------------------------------------
# Общие помощники
# ------------------------------------------------------------------------------


def _service(context: ContextTypes.DEFAULT_TYPE) -> NspdService:
    return context.application.bot_data["nspd"]


def _settings(context: ContextTypes.DEFAULT_TYPE) -> Settings:
    return context.application.bot_data["settings"]


def _activity(context: ContextTypes.DEFAULT_TYPE) -> Activity:
    return context.application.bot_data["activity"]


def _is_short(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return (context.chat_data or {}).get("view_mode") == "short"


def _remember(context: ContextTypes.DEFAULT_TYPE, kad_number: str) -> None:
    if context.chat_data is None:
        return
    history: list[str] = context.chat_data.setdefault("history", [])
    if kad_number in history:
        history.remove(kad_number)
    history.insert(0, kad_number)
    del history[HISTORY_SIZE:]


def _btn(text: str, action: str, kad_number: str) -> InlineKeyboardButton:
    # callback_data ограничен 64 байтами — кадастровый номер укладывается с запасом
    return InlineKeyboardButton(text, callback_data=f"{action}:{kad_number}")


def card_keyboard(obj: NspdObject) -> InlineKeyboardMarkup:
    cad = obj.cad_number
    links = [InlineKeyboardButton("🗺 НСПД", url=nspd_url(cad))]
    if maps_url := yandex_maps_url(obj):
        links.append(InlineKeyboardButton("📍 Яндекс.Карты", url=maps_url))

    navigation = []
    if obj.parent_cad_number:
        navigation.append(_btn("🏢 Здание", "card", obj.parent_cad_number))
    elif obj.has_related_lookup:
        navigation.append(_btn("🔗 Связанные объекты", "rel", cad))
    navigation.append(_btn("🔄 Обновить", "ref", cad))

    return InlineKeyboardMarkup([
        links,
        navigation,
        [_btn("💰 Займ под залог", "loan", cad), _btn("⚡ Срочный выкуп", "buy", cad)],
        [_btn("📄 JSON", "json", cad)],
    ])


def numbers_keyboard(numbers: list[str], *, per_row: int = 2, limit: int = 20) -> InlineKeyboardMarkup | None:
    buttons = [_btn(cad, "card", cad) for cad in numbers[:limit]]
    if not buttons:
        return None
    return InlineKeyboardMarkup([buttons[i : i + per_row] for i in range(0, len(buttons), per_row)])


async def _safe_edit(message: Message, text: str, **kwargs) -> None:
    try:
        await message.edit_text(text, **kwargs)
    except BadRequest as exc:
        if "not modified" not in str(exc).lower():
            raise


async def _render_card(
    message: Message,
    kad_number: str,
    context: ContextTypes.DEFAULT_TYPE,
    *,
    force: bool = False,
) -> None:
    """Запрашивает объект и выводит карточку в уже отправленное сообщение `message`."""
    service, activity = _service(context), _activity(context)
    cached = not force and service.is_cached(kad_number)
    started = time.monotonic()
    try:
        obj = await service.get_object(kad_number, force=force)
    except NspdNotFound:
        activity.lookup(kad_number, not_found=True, seconds=time.monotonic() - started, cached=cached, refresh=force)
        await _safe_edit(
            message,
            f"Объект <code>{esc(kad_number)}</code> в НСПД не найден.\n"
            "Проверьте номер — возможно, объект снят с учёта или номер введён с ошибкой.",
        )
        return
    except NspdUnavailable:
        activity.lookup(kad_number, seconds=time.monotonic() - started, refresh=force)
        await _safe_edit(
            message,
            f"НСПД сейчас не отвечает на запрос по <code>{esc(kad_number)}</code>.\n"
            "Попробуйте повторить через минуту.",
            reply_markup=InlineKeyboardMarkup([[_btn("🔄 Повторить", "ref", kad_number)]]),
        )
        return

    activity.lookup(kad_number, obj=obj, seconds=time.monotonic() - started, cached=cached, refresh=force)
    _remember(context, obj.cad_number)
    await _safe_edit(message, format_card(obj, short=_is_short(context)), reply_markup=card_keyboard(obj))


async def _send_card(message: Message, kad_number: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    status = await message.reply_text(f"⏳ Запрашиваю <code>{esc(kad_number)}</code> в НСПД…")
    await _render_card(status, kad_number, context)


# ------------------------------------------------------------------------------
# Доступ и ошибки
# ------------------------------------------------------------------------------


async def track_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Самый первый обработчик: запоминает, кто пишет, и пишет команды в журнал."""
    _activity(context).track(update)


async def access_guard(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Пропускает только пользователей из ALLOWED_USERS (если список задан)."""
    user = update.effective_user
    if _settings(context).is_allowed(user.id if user else None, user.username if user else None):
        return

    _activity(context).denied()
    if update.callback_query:
        await update.callback_query.answer("Нет доступа", show_alert=True)
    elif update.effective_message and user:
        await update.effective_message.reply_text(
            f"Доступ к боту ограничен.\nВаш Telegram ID: <code>{user.id}</code> — "
            "передайте его администратору бота."
        )
    raise ApplicationHandlerStop


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Трассировка — в файл, в консоль — одна строка через журнал действий
    logger.error("Ошибка при обработке обновления", exc_info=context.error, extra={"console": False})
    _activity(context).error(context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text(
                "Произошла внутренняя ошибка. Попробуйте, пожалуйста, ещё раз."
            )
        except Exception:  # noqa: BLE001 — не даём обработчику ошибок упасть
            pass


# ------------------------------------------------------------------------------
# Команды
# ------------------------------------------------------------------------------


async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    name = esc(user.first_name) if user and user.first_name else "коллега"
    await update.effective_message.reply_text(
        f"Здравствуйте, {name}!\n\n"
        "Я показываю сведения об объектах недвижимости из НСПД по кадастровому номеру: "
        "тип, адрес, площадь, назначение, кадастровую стоимость, связанные объекты, "
        "а также готовлю тексты предложений для инвесторов.\n\n"
        f"Пришлите кадастровый номер, например: <code>{EXAMPLE_NUMBER}</code>\n"
        "Справка — /help"
    )


async def help_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.effective_message.reply_text(HELP_TEXT)


async def short_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["view_mode"] = "short"
    await update.effective_message.reply_text(
        "Включён <b>краткий</b> формат карточек. Вернуть подробный — /full"
    )


async def full_mode_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.chat_data["view_mode"] = "full"
    await update.effective_message.reply_text("Включён <b>подробный</b> формат карточек.")


async def history_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    history: list[str] = context.chat_data.get("history", [])
    if not history:
        await update.effective_message.reply_text("История запросов пока пуста.")
        return
    await update.effective_message.reply_text(
        "<b>Последние запросы</b> — нажмите, чтобы открыть карточку:",
        reply_markup=numbers_keyboard(history),
    )


async def pledge_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _ask_pledge_numbers(update.effective_message, context, first=None)


async def cancel_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if context.chat_data.pop(FORM_KEY, None) is not None:
        await update.effective_message.reply_text("Анкета отменена, предложение не сформировано.")
    elif context.chat_data.pop(PLEDGE_KEY, None) is not None:
        await update.effective_message.reply_text("Ввод объектов для залога отменён.")
    else:
        await update.effective_message.reply_text("Отменять нечего.")


# ------------------------------------------------------------------------------
# Займ под залог нескольких объектов
# ------------------------------------------------------------------------------


async def _ask_pledge_numbers(
    message: Message, context: ContextTypes.DEFAULT_TYPE, *, first: str | None
) -> None:
    """Включает режим ожидания списка номеров для общего предложения по залогу."""
    context.chat_data[PLEDGE_KEY] = [first] if first else []
    _activity(context).action(
        f"займ под залог, несколько объектов{f' (первый {first})' if first else ''} · ждёт номера"
    )
    already = f"Объект <code>{esc(first)}</code> уже в списке.\n" if first else ""
    await message.reply_text(
        "<b>Займ под залог нескольких объектов</b>\n\n"
        f"{already}"
        f"Пришлите кадастровые номера {'остальных ' if first else ''}объектов, "
        "каждый с новой строки — я соберу одно предложение по всем объектам сразу.",
        reply_markup=InlineKeyboardMarkup([[_btn("✖ Отмена", "plcancel", "-")]]),
    )


async def _finish_pledge(message: Message, numbers: list[str], context: ContextTypes.DEFAULT_TYPE) -> None:
    pending: list[str] = context.chat_data.get(PLEDGE_KEY) or []
    if not numbers:
        await message.reply_text(
            "Не удалось распознать кадастровые номера.\n"
            "Пришлите их каждый с новой строки или нажмите /cancel, чтобы отменить.",
        )
        return

    context.chat_data.pop(PLEDGE_KEY, None)
    max_batch = _settings(context).max_batch
    all_numbers = list(dict.fromkeys(pending + numbers))
    skipped = all_numbers[max_batch:]
    all_numbers = all_numbers[:max_batch]

    status = await message.reply_text(f"⏳ Запрашиваю в НСПД объектов для залога: {len(all_numbers)}…")
    started = time.monotonic()
    results = await _fetch_many(context, all_numbers)
    objects = [obj for _, obj, _ in results if obj is not None]
    failed = [(kad, err) for kad, obj, err in results if obj is None]
    _activity(context).pledge(all_numbers, len(objects), time.monotonic() - started)

    if not objects:
        await _safe_edit(
            status,
            "Не удалось получить данные ни по одному объекту — предложение не сформировано.\n"
            + "\n".join(f"• <code>{esc(kad)}</code> — {esc(err)}" for kad, err in failed),
        )
        return

    for obj in reversed(objects):
        _remember(context, obj.cad_number)

    report = [
        f"Данные из НСПД получены: объектов {len(objects)} из {len(all_numbers)}. "
        "Осталось ответить на несколько вопросов для текста предложения."
    ]
    if failed:
        report.append("\n<b>Не вошли в предложение:</b>")
        report += [f"• <code>{esc(kad)}</code> — {esc(err)}" for kad, err in failed]
    if skipped:
        report.append(f"\n<i>Обработаны первые {max_batch} номеров, пропущено: {len(skipped)}.</i>")
    await _safe_edit(status, fit_message("\n".join(report)))

    has_premises = any(obj.kind is ObjectKind.PREMISES for obj in objects)
    await _start_form(message, context, "loan", [obj.cad_number for obj in objects], has_premises)


# ------------------------------------------------------------------------------
# Анкета: вопросы вместо пустых граф в предложении
# ------------------------------------------------------------------------------

_FORM_TITLES = {"loan": "Займ под залог", "buyout": "Срочный выкуп"}


def _question_text(form: OfferForm) -> str:
    question = form.current
    assert question is not None
    lines = [f"<b>{_FORM_TITLES[form.kind]}</b> · вопрос {form.progress}", "", f"<b>{esc(question.prompt)}</b>"]
    if previous := form.answers.get(question.key):
        lines.append(f"Сейчас: {esc(previous)}")
    lines.append("")
    lines.append("<i>Напишите ответ сообщением" + (" или выберите вариант" if question.options else "") + ".</i>")
    return "\n".join(lines)


def _question_keyboard(form: OfferForm) -> InlineKeyboardMarkup:
    question = form.current
    assert question is not None
    step = form.step
    options = [InlineKeyboardButton(text, callback_data=f"fo:{step}.{i}") for i, text in enumerate(question.options)]
    # Короткие варианты — по два в ряд, длинные — по одному
    rows: list[list[InlineKeyboardButton]] = []
    for button in options:
        if rows and len(rows[-1]) == 1 and len(rows[-1][0].text) <= 16 and len(button.text) <= 16:
            rows[-1].append(button)
        else:
            rows.append([button])

    navigation = [InlineKeyboardButton("Пропустить ⏭", callback_data=f"fs:{step}")]
    if step > 0:
        navigation.insert(0, InlineKeyboardButton("◀ Назад", callback_data=f"fb:{step}"))
    rows.append(navigation)
    rows.append([
        InlineKeyboardButton("✍ Остальное заполню сам", callback_data=f"ff:{step}"),
        InlineKeyboardButton("✖ Отмена", callback_data="fx:-"),
    ])
    return InlineKeyboardMarkup(rows)


async def _start_form(
    message: Message,
    context: ContextTypes.DEFAULT_TYPE,
    kind: OfferKind,
    numbers: list[str],
    has_premises: bool,
) -> None:
    form = OfferForm(kind=kind, numbers=numbers, has_premises=has_premises)
    context.chat_data[FORM_KEY] = form
    _activity(context).action(f"анкета «{_FORM_TITLES[kind]}» · вопросов {len(form.questions)}")
    await _ask_question(message, context, form)


async def _ask_question(message: Message, context: ContextTypes.DEFAULT_TYPE, form: OfferForm) -> None:
    if form.done:
        await _finish_form(message, context, form)
        return
    sent = await message.reply_text(_question_text(form), reply_markup=_question_keyboard(form))
    form.message_id = sent.message_id


async def _close_question(
    context: ContextTypes.DEFAULT_TYPE, chat_id: int, form: OfferForm, note: str, question_text: str
) -> None:
    """Убирает кнопки с отвеченного вопроса и дописывает, чем он закончился."""
    if form.message_id is None:
        return
    try:
        await context.bot.edit_message_text(
            f"{question_text.rsplit(chr(10) * 2, 1)[0]}\n\n{note}",
            chat_id=chat_id,
            message_id=form.message_id,
            reply_markup=None,
        )
    except BadRequest as exc:  # сообщение могли удалить — анкета от этого не ломается
        logger.debug("Не удалось обновить вопрос анкеты: %s", exc)


async def _answer_form_text(message: Message, context: ContextTypes.DEFAULT_TYPE, form: OfferForm) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    question_text = _question_text(form)
    value = form.answer(text)
    await _close_question(context, message.chat_id, form, f"✅ {esc(value)}", question_text)
    await _ask_question(message, context, form)


async def _form_callback(query, action: str, payload: str, context: ContextTypes.DEFAULT_TYPE) -> None:
    form: OfferForm | None = context.chat_data.get(FORM_KEY)
    message = query.message

    if action == "fx":
        context.chat_data.pop(FORM_KEY, None)
        _activity(context).action("отменил анкету")
        await query.answer("Отменено")
        await _safe_edit(message, "Анкета отменена, предложение не сформировано.")
        return

    step_text, _, option = payload.partition(".")
    if form is None or not step_text.isdigit() or int(step_text) != form.step or form.done:
        await query.answer("Этот вопрос уже не актуален", show_alert=False)
        return

    await query.answer()
    question_text = _question_text(form)
    if action == "fo":
        question = form.current
        try:
            choice = question.options[int(option)]
        except (ValueError, IndexError):
            return
        value = form.answer(choice)
        note = f"✅ {esc(value)}"
    elif action == "fs":
        form.skip()
        note = "⏭ Пропущено"
    elif action == "fb":
        form.back()
        note = "◀ Вернулись к предыдущему вопросу"
    elif action == "ff":
        form.finish_now()
        note = "✍ Остальные графы останутся пустыми"
    else:
        return

    await _close_question(context, message.chat_id, form, note, question_text)
    await _ask_question(message, context, form)


async def _finish_form(message: Message, context: ContextTypes.DEFAULT_TYPE, form: OfferForm) -> None:
    context.chat_data.pop(FORM_KEY, None)
    results = await _fetch_many(context, form.numbers)
    objects = [obj for _, obj, _ in results if obj is not None]
    if not objects:
        await message.reply_text("Не удалось получить данные объектов из НСПД — попробуйте ещё раз позже.")
        return

    if form.kind == "loan":
        texts = format_pledge_offer(objects, form.answers)
        title = "займ под залог" if len(objects) == 1 else f"займ под залог, объектов {len(objects)}"
    else:
        texts = [format_offer(objects[0], "buyout", form.answers)]
        title = "срочный выкуп"

    _activity(context).offer(
        title, [obj.cad_number for obj in objects], f"{form.filled}/{len(form.questions)}"
    )
    await message.reply_text("Готово! Текст предложения — ниже, его можно переслать инвесторам.")
    for text in texts:
        await message.reply_text(text)


# ------------------------------------------------------------------------------
# Текстовые сообщения
# ------------------------------------------------------------------------------


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    numbers = extract_kad_numbers(message.text or "")

    form = context.chat_data.get(FORM_KEY) if context.chat_data is not None else None
    if form is not None:
        await _answer_form_text(message, context, form)
        return

    if context.chat_data is not None and PLEDGE_KEY in context.chat_data:
        await _finish_pledge(message, numbers, context)
        return

    if not numbers:
        _activity(context).unrecognized(message.text or "")
        await message.reply_text(
            "Не удалось распознать кадастровый номер.\n\n"
            f"Пришлите его в формате <code>{EXAMPLE_NUMBER}</code> "
            "(можно с пробелами вместо двоеточий). Справка — /help"
        )
        return

    if len(numbers) == 1:
        await _send_card(message, numbers[0], context)
    else:
        await _send_batch(message, numbers, context)


async def _fetch_many(
    context: ContextTypes.DEFAULT_TYPE, numbers: list[str]
) -> list[tuple[str, NspdObject | None, str | None]]:
    """Параллельно запрашивает номера; ошибки возвращаются текстом, а не исключением."""
    service = _service(context)

    async def fetch(kad: str) -> tuple[str, NspdObject | None, str | None]:
        try:
            return kad, await service.get_object(kad), None
        except NspdNotFound:
            return kad, None, "не найден в НСПД"
        except NspdUnavailable:
            return kad, None, "НСПД не ответила"

    return list(await asyncio.gather(*(fetch(kad) for kad in numbers)))


async def _send_batch(message: Message, numbers: list[str], context: ContextTypes.DEFAULT_TYPE) -> None:
    max_batch = _settings(context).max_batch
    skipped = len(numbers) - max_batch
    numbers = numbers[:max_batch]

    status = await message.reply_text(f"⏳ Запрашиваю в НСПД объектов: {len(numbers)}…")
    started = time.monotonic()
    results = await _fetch_many(context, numbers)
    found = [obj.cad_number for _, obj, _ in results if obj is not None]
    _activity(context).batch(numbers, len(found), time.monotonic() - started)
    for cad in reversed(found):
        _remember(context, cad)

    lines = [f"<b>Найдено {len(found)} из {len(numbers)}</b>"]
    lines += [format_batch_line(i, kad, obj, err) for i, (kad, obj, err) in enumerate(results, 1)]
    if skipped > 0:
        lines.append(f"\n<i>Обработаны первые {max_batch} номеров, пропущено: {skipped}.</i>")

    await _safe_edit(status, fit_message("\n\n".join(lines)), reply_markup=numbers_keyboard(found))

    filename = f"nspd_{datetime.now():%Y%m%d_%H%M}.csv"
    await message.reply_document(
        InputFile(build_csv(results), filename=filename),
        caption="Таблица по всем объектам (открывается в Excel)",
    )


# ------------------------------------------------------------------------------
# Inline-кнопки
# ------------------------------------------------------------------------------


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action, _, kad_number = (query.data or "").partition(":")
    if not kad_number:
        await query.answer()
        return

    message = query.message

    if action in ("fo", "fs", "fb", "ff", "fx"):
        await _form_callback(query, action, kad_number, context)
        return

    if action == "card":
        await query.answer()
        await _send_card(message, kad_number, context)
        return

    if action == "ref":
        await query.answer("Обновляю данные…")
        await _render_card(message, kad_number, context, force=True)
        return

    activity = _activity(context)

    if action == "loan":
        await query.answer()
        activity.action(f"займ под залог · {kad_number} · выбирает: один или несколько")
        await message.reply_text(
            f"Займ под залог по объекту <code>{esc(kad_number)}</code>.\n"
            "Сколько объектов предлагается к залогу?",
            reply_markup=InlineKeyboardMarkup([[
                _btn("1️⃣ Один объект", "loan1", kad_number),
                _btn("🔢 Несколько объектов", "loanm", kad_number),
            ]]),
        )
        return

    if action == "loanm":
        await query.answer()
        await _ask_pledge_numbers(message, context, first=kad_number)
        return

    if action == "plcancel":
        context.chat_data.pop(PLEDGE_KEY, None)
        activity.action("отменил ввод объектов для залога")
        await query.answer("Отменено")
        await _safe_edit(message, "Ввод объектов для залога отменён.")
        return

    # Остальные действия работают с уже полученным объектом
    try:
        obj = await _service(context).get_object(kad_number)
    except NspdNotFound:
        await query.answer("Объект не найден в НСПД", show_alert=True)
        return
    except NspdUnavailable:
        await query.answer("НСПД сейчас не отвечает, попробуйте позже", show_alert=True)
        return

    await query.answer()

    if action == "rel":
        related = await _service(context).get_related(obj)
        activity.action(f"связанные объекты · {kad_number} · найдено {len(related)}")
        await message.reply_text(format_related(obj, related), reply_markup=numbers_keyboard(related))
    elif action in ("loan1", "buy"):
        kind: OfferKind = "loan" if action == "loan1" else "buyout"
        await _start_form(message, context, kind, [obj.cad_number], obj.kind is ObjectKind.PREMISES)
    elif action == "json":
        activity.action(f"скачал JSON · {kad_number}")
        payload = json.dumps(obj.raw, ensure_ascii=False, indent=2).encode("utf-8")
        await message.reply_document(
            InputFile(payload, filename=f"{kad_number.replace(':', '_')}.json"),
            caption=f"Ответ НСПД для <code>{esc(kad_number)}</code>",
        )
    else:
        logger.warning("Неизвестное действие кнопки: %r", query.data)
