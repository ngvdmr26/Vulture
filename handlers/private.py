"""Aiogram 3.x router for private (DM) commands.

* ``/start``   – Welcome message.
* ``/dossier`` – Generate a personal dossier card for a selected group.
"""

from __future__ import annotations

import logging
from datetime import datetime

from aiogram import Bot, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Message as TgMessage,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select, distinct

from database import Group, Message as DbMessage, get_session
from engine.card_generator import render_dossier
from engine.graph import get_user_metrics
from engine.profiler import generate_profile

logger = logging.getLogger(__name__)
router = Router(name="private")


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart(), F.chat.type == "private")
async def _start(message: TgMessage) -> None:
    """Send the welcome message."""
    text = (
        "🦅 **VULTURE** — Social Graph Intelligence\n\n"
        "I silently observe group dynamics and build psychological\n"
        "profiles based on interaction patterns.\n\n"
        "_No messages are stored. Only metadata._\n\n"
        "**Commands**\n"
        "/dossier — Generate your personal dossier card\n"
        "/start — Show this message\n\n"
        "Add me to a group to begin surveillance."
    )
    await message.answer(text, parse_mode="Markdown")


# ---------------------------------------------------------------------------
# /dossier
# ---------------------------------------------------------------------------

@router.message(Command("dossier"), F.chat.type == "private")
async def _dossier(message: TgMessage, bot: Bot) -> None:
    """Prompt the user to select a group, then generate their dossier."""
    if message.from_user is None:
        return
    user_id = message.from_user.id

    # Find groups where this user has activity
    session = get_session()
    try:
        result = await session.execute(
            select(distinct(DbMessage.chat_id)).where(DbMessage.user_id == user_id)
        )
        chat_ids: list[int] = [row for row in result.scalars().all()]

        if not chat_ids:
            await message.answer(
                "📡 No telemetry found.  I need to observe you in a group first."
            )
            return

        # Fetch group titles
        result = await session.execute(
            select(Group).where(Group.chat_id.in_(chat_ids), Group.is_active == True)  # noqa: E712
        )
        groups = list(result.scalars().all())
    finally:
        await session.close()

    if not groups:
        # User has messages but the groups may have been deactivated
        await message.answer(
            "📡 No active groups found for your account.  "
            "Make sure the bot is still present in the group."
        )
        return

    if len(groups) == 1:
        # Only one group – generate directly
        await _generate_and_send_dossier(message, bot, user_id, groups[0].chat_id)
        return

    # Multiple groups – present selector
    builder = InlineKeyboardBuilder()
    for g in groups:
        label = g.title or f"Chat {g.chat_id}"
        builder.button(text=label, callback_data=f"dossier:{g.chat_id}")
    builder.adjust(1)

    await message.answer(
        "📂 Select a group for your dossier:",
        reply_markup=builder.as_markup(),
    )


# ---------------------------------------------------------------------------
# Callback: group selection
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("dossier:"))
async def _on_group_selected(callback: CallbackQuery, bot: Bot) -> None:
    """Handle group selection and generate the dossier card."""
    if callback.from_user is None or callback.message is None:
        await callback.answer("⚠️ Something went wrong.")
        return

    try:
        chat_id = int(callback.data.split(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        await callback.answer("⚠️ Invalid selection.")
        return

    await callback.answer()  # dismiss spinner
    await _generate_and_send_dossier(
        callback.message, bot, callback.from_user.id, chat_id,
    )


# ---------------------------------------------------------------------------
# Dossier generation helper
# ---------------------------------------------------------------------------

async def _generate_and_send_dossier(
    target: TgMessage,
    bot: Bot,
    user_id: int,
    chat_id: int,
) -> None:
    """Run the full analytics → profile → render pipeline and send the card."""
    await bot.send_chat_action(target.chat.id, "upload_photo")

    # 1. Metrics
    metrics = await get_user_metrics(chat_id, user_id, days=30)
    if metrics is None:
        await target.answer(
            "📡 Insufficient telemetry for your profile in this group.\n"
            "I need more interaction data before I can compile a dossier."
        )
        return

    # 2. Psychological profile
    profile = await generate_profile(metrics)

    # 3. Merge into a single dict for the card renderer
    card_data = {**metrics, **profile}

    # 4. Render PNG
    me = await bot.get_me()
    bot_username = me.username or "VultureBot"
    buf = await render_dossier(card_data, bot_username=bot_username)

    # 5. Send
    photo = BufferedInputFile(buf.read(), filename="dossier.png")
    await target.answer_photo(
        photo,
        caption=f"🦅 Dossier for **@{metrics['username']}** — _{profile['rank_title']}_",
        parse_mode="Markdown",
    )
