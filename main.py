"""Vulture — Telegram Social Graph Intelligence Bot.

Entrypoint: initialises the database, registers routers, starts polling.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import get_settings
from database import init_db
from engine.scheduler import start_weekly_purge_task
from handlers.group import router as group_router
from handlers.private import router as private_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

async def main() -> None:
    settings = get_settings()

    if not settings.BOT_TOKEN:
        logger.error("BOT_TOKEN is not configured – set it in .env")
        return

    session = AiohttpSession(proxy=settings.PROXY_URL) if settings.PROXY_URL else None

    bot = Bot(
        token=settings.BOT_TOKEN,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()

    # Fetch bot identity and cache it
    me = await bot.get_me()
    logger.info("Starting Vulture as @%s (id=%s)", me.username, me.id)

    # Initialise database (creates tables on first run)
    await init_db()

    # Register routers (private first so DM commands are matched before group filters)
    dp.include_router(private_router)
    dp.include_router(group_router)

    # Register bot commands for the Telegram menu
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск бота / справка"),
        BotCommand(command="top", description="Рейтинг влияния участников"),
        BotCommand(command="sync", description="Анализ связи двух участников"),
        BotCommand(command="pulse", description="Сводка аномалий (админы)"),
        BotCommand(command="dossier", description="Персональное досье"),
        BotCommand(command="feedback", description="Связь с разработчиком (баги, идеи)"),
    ])

    # Запускаем фоновый планировщик «Судного дня»
    asyncio.create_task(start_weekly_purge_task(bot))

    # Start long-polling
    try:
        await dp.start_polling(
            bot,
            allowed_updates=[
                "message",
                "callback_query",
                "my_chat_member",
                "message_reaction",
            ],
        )
    finally:
        await bot.session.close()
        logger.info("Vulture shut down.")


if __name__ == "__main__":
    asyncio.run(main())