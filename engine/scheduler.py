"""Background scheduler for Vulture — Sunday Purge + Saturday Betting.

Schedule (UTC):
  Saturday 17:00 UTC (20:00 МСК) — Open Totalizator betting
  Sunday   17:00 UTC (20:00 МСК) — Calculate bet winners, award prizes
  Sunday   19:00 UTC (22:00 МСК) — Send weekly purge report
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from database import Group, PurgeBet, UserGroupStats, get_session
from engine.graph import get_weekly_purge

logger = logging.getLogger(__name__)


async def _get_active_groups() -> list[Group]:
    try:
        async with get_session() as session:
            res = await session.execute(select(Group).where(Group.is_active.is_(True)))
            return list(res.scalars().all())
    except Exception:
        logger.exception("Failed to query active groups")
        return []


async def _dispatch_purge_to_group(bot: Bot, grp: Group) -> str:
    """Build and send Sunday Purge report. Returns 'sent'/'skipped'/'failed'."""
    chat_id = grp.chat_id
    try:
        report = await get_weekly_purge(chat_id)
    except Exception:
        logger.exception("Error computing weekly purge for chat %s", chat_id)
        return "failed"

    if report is None:
        logger.debug("Weekly purge skipped for chat %s (insufficient data)", chat_id)
        return "skipped"

    text = (
        "🚨 <b>VULTURE // СУДНЫЙ ДЕНЬ: ИТОГИ НЕДЕЛИ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👑 <b>Серый кардинал:</b> {report['top_influencer']}\n"
        f"🤡 <b>Клоун недели:</b> {report['clown_of_the_week']}\n"
        f"💀 <b>Главный игнор недели:</b> {report['neglected']}\n"
        f"🔗 <b>Скрытый альянс:</b> {report['secret_pair']}\n\n"
        "<i>Протокол наблюдения запечатан на следующие 7 дней.</i>"
    )

    try:
        await bot.send_message(chat_id, text, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.warning("Telegram API refused purge to chat %s: %s", chat_id, exc)
        return "failed"
    except Exception:
        logger.exception("Unexpected error sending purge to chat %s", chat_id)
        return "failed"

    try:
        async with get_session() as session:
            res = await session.execute(select(Group).where(Group.chat_id == chat_id))
            db_group = res.scalar_one_or_none()
            if db_group:
                db_group.last_report_at = datetime.now(timezone.utc).replace(tzinfo=None)
                await session.commit()
    except Exception:
        logger.warning("Failed to update last_report_at for chat %s", chat_id)

    return "sent"


def build_betting_keyboard(week_key: str | None = None) -> InlineKeyboardBuilder:
    """Build inline keyboard for betting."""
    if week_key is None:
        week_key = datetime.now(timezone.utc).strftime("%Y-W%W")
    builder = InlineKeyboardBuilder()
    builder.button(text="👑 Серый кардинал", callback_data=f"bet:top_influencer:{week_key}")
    builder.button(text="🤡 Клоун недели", callback_data=f"bet:clown_of_the_week:{week_key}")
    builder.button(text="💀 Главный игнор", callback_data=f"bet:neglected:{week_key}")
    builder.adjust(1)
    return builder


async def send_betting_invitation(bot: Bot, chat_id: int) -> bool:
    """Send betting announcement to a chat."""
    week_key = datetime.now(timezone.utc).strftime("%Y-W%W")
    builder = build_betting_keyboard(week_key)

    text = (
        "🎰 <b>VULTURE // ТОТАЛИЗАТОР СУДНОГО ДНЯ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Угадай победителей воскресного разбора — и получи право навешивать клейма!\n\n"
        "🏆 <b>Приз для угадавших:</b>\n"
        "• +1 заряд <b>Глушилки</b> (защита от досье-игр)\n"
        "• +1 заряд <b>Чёрной метки</b> (команда /brand)\n"
        "• +1.5 бонусных балла к рейтингу влияния\n\n"
        "Выбери номинацию и назови свою ставку:"
    )

    try:
        await bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=builder.as_markup(),
        )
        return True
    except TelegramAPIError:
        logger.debug("Failed to send betting invitation to chat %s", chat_id)
        return False


def _extract_username(raw: str) -> str:
    """Extract first @username from formatted string like '@user (5 реакций)'."""
    if not raw:
        return ""
    for part in raw.split():
        if part.startswith("@"):
            return part.lstrip("@").rstrip(")")
    return raw.strip("@")


async def _resolve_bets_for_group(bot: Bot, grp: Group, purge_report: dict) -> None:
    """Calculate bet winners and award prizes (Sunday 17:00 UTC)."""
    chat_id = grp.chat_id
    week_key = datetime.now(timezone.utc).strftime("%Y-W%W")

    winners_map = {
        "top_influencer": _extract_username(purge_report.get("top_influencer", "")),
        "clown_of_the_week": _extract_username(purge_report.get("clown_of_the_week", "")),
        "neglected": _extract_username(purge_report.get("neglected", "")),
    }

    try:
        async with get_session() as session:
            res = await session.execute(
                select(PurgeBet).where(
                    PurgeBet.chat_id == chat_id,
                    PurgeBet.week_key == week_key,
                )
            )
            bets = list(res.scalars().all())
    except Exception:
        logger.exception("Failed to fetch bets for chat %s", chat_id)
        return

    if not bets:
        return

    correct_voters: list[tuple[int, str | None]] = []
    for bet in bets:
        actual = winners_map.get(bet.category, "")
        if actual and bet.target_username.lower().strip("@") == actual.lower().strip("@"):
            correct_voters.append((bet.voter_id, bet.voter_username))

    if not correct_voters:
        try:
            await bot.send_message(
                chat_id,
                "🎰 <b>ТОТАЛИЗАТОР // ИТОГИ СТАВОК</b>\n\n"
                "В этот раз никто не угадал. Ставки сгорели.\n"
                "<i>В следующую субботу — новый шанс!</i>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    awarded_lines: list[str] = []
    for voter_id, voter_username in correct_voters:
        try:
            async with get_session() as session:
                res = await session.execute(
                    select(UserGroupStats).where(
                        UserGroupStats.chat_id == chat_id,
                        UserGroupStats.user_id == voter_id,
                    )
                )
                stats = res.scalar_one_or_none()
                if stats is None:
                    stats = UserGroupStats(
                        chat_id=chat_id,
                        user_id=voter_id,
                        username=voter_username,
                        jammer_charges=1,
                    )
                    session.add(stats)
                stats.jammer_charges = (stats.jammer_charges or 0) + 1
                stats.brand_charges = (stats.brand_charges or 0) + 1
                stats.bonus_influence = (stats.bonus_influence or 0.0) + 1.5
                await session.commit()
            name = voter_username or f"id_{voter_id}"
            awarded_lines.append(f"• @{name}")
        except Exception:
            logger.exception("Failed to award prizes to voter %s in chat %s", voter_id, chat_id)

    if awarded_lines:
        lines_str = "\n".join(awarded_lines)
        try:
            await bot.send_message(
                chat_id,
                f"🏆 <b>ТОТАЛИЗАТОР // ПОБЕДИТЕЛИ СТАВОК</b>\n\n"
                f"Угадали правильно и получили награды:\n{lines_str}\n\n"
                f"<b>Приз:</b> +1 🛡 Глушилка · +1 🏷 Чёрная метка · +1.5 ★ к рейтингу",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            logger.debug("Failed to send bet winners to chat %s", chat_id)


async def start_weekly_purge_task(bot: Bot) -> None:
    """Run non-blocking background loop for all scheduled events."""
    logger.info("Background Scheduler activated (Purge + Betting).")
    while True:
        try:
            now = datetime.now(timezone.utc)

            # Saturday 17:00 UTC (20:00 MSK) — Open betting
            if now.weekday() == 5 and now.hour == 17 and now.minute == 0:
                logger.info("Opening Totalizator betting across active groups...")
                groups = await _get_active_groups()
                for grp in groups:
                    try:
                        await send_betting_invitation(bot, grp.chat_id)
                    except Exception:
                        logger.exception("Failed betting open for chat %s", grp.chat_id)
                try:
                    await asyncio.sleep(70)
                except asyncio.CancelledError:
                    break
                continue

            # Sunday 17:00 UTC (20:00 MSK) — Resolve bets
            if now.weekday() == 6 and now.hour == 17 and now.minute == 0:
                logger.info("Resolving Totalizator bets across active groups...")
                groups = await _get_active_groups()
                for grp in groups:
                    try:
                        report = await get_weekly_purge(grp.chat_id)
                        if report:
                            await _resolve_bets_for_group(bot, grp, report)
                    except Exception:
                        logger.exception("Failed bet resolution for chat %s", grp.chat_id)
                try:
                    await asyncio.sleep(70)
                except asyncio.CancelledError:
                    break
                continue

            # Sunday 19:00 UTC (22:00 MSK) — Send weekly purge report
            if now.weekday() == 6 and now.hour == 19 and now.minute == 0:
                logger.info("Executing Sunday Purge protocol across active groups...")
                groups = await _get_active_groups()
                if not groups:
                    logger.info("No active groups found for Sunday Purge.")
                else:
                    stats = {"sent": 0, "skipped": 0, "failed": 0}
                    for grp in groups:
                        result = await _dispatch_purge_to_group(bot, grp)
                        stats[result] += 1
                    logger.info(
                        "Sunday Purge complete: %d sent, %d skipped, %d failed (of %d groups)",
                        stats["sent"], stats["skipped"], stats["failed"], len(groups),
                    )
                try:
                    await asyncio.sleep(70)
                except asyncio.CancelledError:
                    break
                continue

        except asyncio.CancelledError:
            logger.info("Background Scheduler stopped.")
            break
        except Exception:
            logger.exception("Scheduler main loop crashed, recovering in 60s...")
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                break

        try:
            await asyncio.sleep(40)
        except asyncio.CancelledError:
            break