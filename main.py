"""Vulture — Telegram Social Graph Intelligence Bot.

Entrypoint: initialises the database, registers routers, starts polling.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher

from config import get_settings
from database import init_db
from handlers.group import router as group_router
from handlers.private import router as private_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    settings = get_settings()

    if not settings.BOT_TOKEN or settings.BOT_TOKEN == "your-telegram-bot-token":
        logger.error("BOT_TOKEN is not configured – set it in .env")
        return

    bot = Bot(token=settings.BOT_TOKEN)
    dp = Dispatcher()

    # Fetch bot identity and cache it
    me = await bot.get_me()
    logger.info("Starting Vulture as @%s (id=%s)", me.username, me.id)

    # Initialise database (creates tables on first run)
    await init_db()

    # Register routers
    dp.include_router(group_router)
    dp.include_router(private_router)

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
