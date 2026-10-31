"""Сборка и запуск Telegram-приложения."""

from __future__ import annotations

import asyncio
import logging
import sys
from logging.handlers import RotatingFileHandler

from telegram import BotCommand, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    Defaults,
    MessageHandler,
    PersistenceInput,
    PicklePersistence,
    TypeHandler,
    filters,
)

from . import __version__, handlers
from .activity import Activity, ConsoleFilter, ConsoleFormatter
from .config import Settings, load_settings
from .nspd_service import NspdService

logger = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("start", "Начать"),
    BotCommand("help", "Справка"),
    BotCommand("short", "Краткие карточки"),
    BotCommand("full", "Подробные карточки"),
    BotCommand("history", "Последние запросы"),
    BotCommand("pledge", "Залог нескольких объектов"),
    BotCommand("cancel", "Отменить ввод номеров"),
]


def setup_logging(settings: Settings) -> None:
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")

    file_handler = RotatingFileHandler(
        settings.log_dir / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    # В консоли — только читаемый журнал действий и предупреждения, подробности — в файле
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(ConsoleFormatter())
    console_handler.addFilter(ConsoleFilter())

    logging.basicConfig(level=settings.log_level, handlers=[console_handler, file_handler], force=True)
    # httpx пишет в INFO каждый long-polling запрос к Telegram — это шум
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


HEARTBEAT_SECONDS = 60 * 60


async def _heartbeat(activity: Activity) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        activity.heartbeat()


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)
    me = await application.bot.get_me()
    settings: Settings = application.bot_data["settings"]
    activity: Activity = application.bot_data["activity"]
    activity.startup(me.username, __version__, len(settings.allowed_ids) + len(settings.allowed_usernames))
    application.bot_data["heartbeat"] = asyncio.create_task(_heartbeat(activity))


async def _post_shutdown(application: Application) -> None:
    task = application.bot_data.pop("heartbeat", None)
    if task is not None:
        task.cancel()
    application.bot_data["activity"].shutdown()


def build_application(settings: Settings) -> Application:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    persistence = PicklePersistence(
        filepath=settings.data_dir / "state.pickle",
        store_data=PersistenceInput(bot_data=False, user_data=False, callback_data=False),
        update_interval=30,
    )

    builder = (
        Application.builder()
        .token(settings.token)
        .defaults(Defaults(parse_mode=ParseMode.HTML, link_preview_options=LinkPreviewOptions(is_disabled=True)))
        .persistence(persistence)
        .concurrent_updates(True)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
    )
    if settings.proxy:
        builder = builder.proxy(settings.proxy).get_updates_proxy(settings.proxy)
    application = builder.build()

    application.bot_data["settings"] = settings
    application.bot_data["activity"] = Activity()
    application.bot_data["nspd"] = NspdService(
        timeout=settings.nspd_timeout,
        retries=settings.nspd_retries,
        concurrency=settings.nspd_concurrency,
        cache_ttl=settings.cache_ttl_seconds,
    )

    application.add_handler(TypeHandler(Update, handlers.track_update), group=-2)
    if settings.access_restricted:
        application.add_handler(TypeHandler(Update, handlers.access_guard), group=-1)

    application.add_handler(CommandHandler("start", handlers.start_handler))
    application.add_handler(CommandHandler("help", handlers.help_handler))
    application.add_handler(CommandHandler("short", handlers.short_mode_handler))
    application.add_handler(CommandHandler("full", handlers.full_mode_handler))
    application.add_handler(CommandHandler("history", handlers.history_handler))
    application.add_handler(CommandHandler("pledge", handlers.pledge_handler))
    application.add_handler(CommandHandler("cancel", handlers.cancel_handler))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text_handler))
    application.add_handler(CallbackQueryHandler(handlers.callback_handler))
    application.add_error_handler(handlers.error_handler)

    return application


def main() -> None:
    settings = load_settings()
    setup_logging(settings)
    application = build_application(settings)

    # В Python 3.12+ в главном потоке нет «текущего» event loop,
    # а python-telegram-bot 21.x ожидает, что он уже создан.
    asyncio.set_event_loop(asyncio.new_event_loop())
    application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)
