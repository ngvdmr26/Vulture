"""Vulture — Telegram Social Graph Intelligence Bot.

Entrypoint: initialises the database, registers routers, starts polling.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.types import BotCommand

from config import get_settings
from database import Group, init_db, get_session
from engine.scheduler import start_weekly_purge_task
from handlers.feedback import router as feedback_router
from handlers.group import router as group_router
from handlers.private import router as private_router
from sqlalchemy import select

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger(__name__)

_V2_BROADCAST_TEXT = (
    "🦅 <b>VULTURE v2.0: ПРОТОКОЛ ПЕРЕХВАТА АКТИВИРОВАН</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
    "Сеть зафиксировала аномальную активность. Пока вы были заняты своими делами, "
    "система получила глубокое обновление.\n\n"
    "Никаких скучных отчётов. Вот что изменилось прямо сейчас:\n\n"
    "🎲 <b>«Угадайка» досье (/dossier):</b> Теперь вызов досье на любого участника "
    "превращается в общую охоту! Вся группа видит тактическую плашку и пытается угадать "
    "объект притяжения.\n"
    "🛡 <i>Хотите защитить свои секреты от чужих глаз?</i> Пара кликов — и в дело вступает "
    "<b>Глушилка</b> (всем выдано по 2 стартовых заряда!). Она зашифрует ваши данные, "
    "прикроет следы и оставит в чате едкую цитату вместо улик. Пусть гадают дальше!\n\n"
    "📡 <b>Новая мини-игра /scan («Сканирование эфира»):</b> Рискните 1 зарядом Глушилки "
    "прямо в чате, чтобы запустить анимированный хакерский терминал! Ловите пустые частоты, "
    "фармите до +3 зарядов за джекпот или рискуйте поймать ироничный статус-позывной на 24 часа.\n\n"
    "🎯 <b>Субботний Тотализатор (/bets):</b> Хватит просто болтать — пора делать ставки "
    "на тех, кто реально тащит чат. Каждую субботу в 20:00 открывается окно ставок по топ-6 "
    "самым активным участникам.\n"
    "💡 <i>Зачем это нужно?</i> Победители тотализатора получают эксклюзивную "
    "<b>Чёрную метку (/brand)</b> и право лично переписать статус любого участника на 72 часа! "
    "Ошиблись кандидатом? Не беда, голос можно менять прямо на лету. "
    "Потеряли сообщение? Просто введите <code>/bets</code> в любой момент.\n\n"
    "📈 <b>Новые роли и сканирование в /top:</b> Алгоритмы стали умнее. Забудьте про "
    "одинаковые ярлыки — теперь каждый получает уникальный киберпанк-архетип, "
    "тренды активности и жёсткую статистику ответов.\n\n"
    "⚡ <i>Сеть перезагружена. Протокол запущен. "
    "Кто первым проверит обновлённый софт на прочность?</i>"
)


async def broadcast_v2_update(bot) -> None:
    """Send the v2.0 announcement to all active groups. Safe fire-and-forget."""
    try:
        async with get_session() as session:
            res = await session.execute(select(Group).where(Group.is_active.is_(True)))
            groups = list(res.scalars().all())
    except Exception:
        logger.exception("broadcast_v2_update: failed to fetch active groups")
        return

    logger.info("broadcast_v2_update: sending to %d groups", len(groups))
    from aiogram.exceptions import TelegramForbiddenError, TelegramAPIError as TgAPIError
    from aiogram.enums import ParseMode as PM

    for grp in groups:
        try:
            await bot.send_message(grp.chat_id, _V2_BROADCAST_TEXT, parse_mode=PM.HTML)
        except TelegramForbiddenError:
            # Bot was removed — mark inactive
            try:
                async with get_session() as session:
                    res = await session.execute(select(Group).where(Group.chat_id == grp.chat_id))
                    g = res.scalar_one_or_none()
                    if g:
                        g.is_active = False
                        await session.commit()
            except Exception:
                pass
            logger.debug("broadcast_v2_update: bot removed from chat %s, marked inactive", grp.chat_id)
        except TgAPIError:
            logger.debug("broadcast_v2_update: failed to send to chat %s", grp.chat_id)
        await asyncio.sleep(0.35)  # ~3 msgs/sec to stay inside Telegram flood limits


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
    dp = Dispatcher(storage=MemoryStorage())

    # Fetch bot identity and cache it
    me = await bot.get_me()
    logger.info("Starting Vulture as @%s (id=%s)", me.username, me.id)

    # Initialise database (creates tables on first run)
    await init_db()

    # Register routers
    # feedback first — its FSM text handler must match before the group text logger
    dp.include_router(feedback_router)
    dp.include_router(private_router)
    dp.include_router(group_router)

    # Register bot commands for the Telegram menu
    await bot.set_my_commands([
        BotCommand(command="start", description="Запуск бота / справка"),
        BotCommand(command="top", description="Рейтинг влияния участников"),
        BotCommand(command="sync", description="Анализ связи двух участников"),
        BotCommand(command="dossier", description="Персональное досье"),
        BotCommand(command="scan", description="Сканирование эфира (мини-игра, 1 заряд Глушилки)"),
        BotCommand(command="brand", description="Навесить статус на участника (Чёрная метка)"),
        BotCommand(command="bets", description="Открыть меню ставок Тотализатора (чтобы не искать сообщение в чате)"),
        BotCommand(command="feedback", description="Связь с разработчиком (баги, идеи)"),
    ])

    # v2.0 broadcast — notify all active groups about the update (non-blocking)
    asyncio.create_task(broadcast_v2_update(bot))

    # Запускаем фоновый планировщик «Судного дня»
    purge_task = asyncio.create_task(start_weekly_purge_task(bot))

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
        purge_task.cancel()
        await bot.session.close()
        logger.info("Vulture shut down.")


if __name__ == "__main__":
    asyncio.run(main())