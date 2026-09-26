"""Background weekly scheduler for Vulture Sunday Purge protocol.

Key resilience guarantees:
- A failure in one group never interrupts delivery to the remaining groups.
- ``get_weekly_purge`` is called inside its own try/except per group.
- ``bot.send_message`` is called inside its own try/except per group.
- Groups with near-zero activity silently return ``None`` from the graph engine
  and are skipped without error.
- Successful deliveries are recorded via ``Group.last_report_at`` to enable
  future dedup / retry logic.
- Summary stats (sent / skipped / failed) are logged after each run.
"""

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from sqlalchemy import select

from database import Group, get_session
from engine.graph import get_weekly_purge

logger = logging.getLogger(__name__)


async def _dispatch_purge_to_group(bot: Bot, grp: Group) -> str:
    """Attempt to build and send a Sunday Purge report for a single group.

    Returns one of: ``"sent"``, ``"skipped"``, ``"failed"``.
    Designed so that **no exception propagates** to the caller.
    """
    chat_id = grp.chat_id

    # --- Phase 1: Build the analytics report ---
    try:
        report = await get_weekly_purge(chat_id)
    except Exception:
        logger.exception("Unexpected error computing weekly purge for chat %s", chat_id)
        return "failed"

    if report is None:
        # Группа с околонулевой активностью — нечего отправлять
        logger.debug("Weekly purge skipped for chat %s (insufficient data)", chat_id)
        return "skipped"

    # --- Phase 2: Format message ---
    text = (
        "🚨 <b>VULTURE // СУДНЫЙ ДЕНЬ: ИТОГИ НЕДЕЛИ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👑 <b>Серый кардинал:</b> {report['top_influencer']}\n"
        f"🤡 <b>Клоун недели:</b> {report['clown_of_the_week']}\n"
        f"💀 <b>Главный игнор недели:</b> {report['neglected']}\n"
        f"🔗 <b>Скрытый альянс:</b> {report['secret_pair']}\n\n"
        "<i>Протокол наблюдения запечатан на следующие 7 дней.</i>"
    )

    # --- Phase 3: Send message ---
    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.warning(
            "Telegram API refused purge delivery to chat %s: %s",
            chat_id, exc,
        )
        return "failed"
    except Exception:
        logger.exception("Unexpected error sending purge to chat %s", chat_id)
        return "failed"

    # --- Phase 4: Record successful delivery ---
    try:
        async with get_session() as session:
            res = await session.execute(select(Group).where(Group.chat_id == chat_id))
            db_group = res.scalar_one_or_none()
            if db_group is not None:
                db_group.last_report_at = datetime.now(timezone.utc).replace(tzinfo=None)
                await session.commit()
    except Exception:
        # Non-critical: delivery already succeeded; just log the bookkeeping failure
        logger.warning("Failed to update last_report_at for chat %s", chat_id)

    return "sent"


async def start_weekly_purge_task(bot: Bot) -> None:
    """Run non-blocking background loop checking for Sunday 19:00 UTC."""
    logger.info("Background Purge Scheduler activated.")
    while True:
        try:
            now = datetime.now(timezone.utc)
            # Воскресенье (weekday == 6), 19:00 UTC (22:00 МСК)
            if now.weekday() == 6 and now.hour == 19 and now.minute == 0:
                logger.info("Executing Sunday Purge protocol across active groups...")

                # --- Fetch groups ---
                try:
                    async with get_session() as session:
                        res = await session.execute(
                            select(Group).where(Group.is_active.is_(True))
                        )
                        groups = list(res.scalars().all())
                except Exception:
                    logger.exception("Failed to query active groups for Sunday Purge")
                    groups = []

                if not groups:
                    logger.info("No active groups found for Sunday Purge.")
                else:
                    # --- Dispatch to each group independently ---
                    stats = {"sent": 0, "skipped": 0, "failed": 0}
                    for grp in groups:
                        result = await _dispatch_purge_to_group(bot, grp)
                        stats[result] += 1

                    logger.info(
                        "Sunday Purge complete: %d sent, %d skipped, %d failed (of %d groups)",
                        stats["sent"], stats["skipped"], stats["failed"], len(groups),
                    )

                # Спим 70 сек, чтобы гарантированно не сработать повторно в ту же минуту
                await asyncio.sleep(70)
        except Exception:
            logger.exception("Scheduler main loop crashed, recovering in 60s...")
            await asyncio.sleep(60)

        await asyncio.sleep(40)