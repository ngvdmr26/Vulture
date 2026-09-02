"""Aiogram 3.x router for group-chat events.

* Silently logs text/media messages (metadata only – no raw text stored).
* Logs ``MessageReactionUpdated`` events.
* Provides the ``/pulse`` admin command.
* Tracks bot membership via ``my_chat_member`` updates.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import (
    ChatMemberUpdated,
    Message as TgMessage,
    MessageReactionUpdated,
)

from database import Group, Message, Reaction, get_session
from engine.graph import get_group_anomalies

logger = logging.getLogger(__name__)
router = Router(name="group")

# Only operate in groups / supergroups
_GROUP_FILTER = F.chat.type.in_({"group", "supergroup"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _resolve_username(user) -> str:  # aiogram User object
    """Return the best available display name, never ``None``."""
    if user is None:
        return "unknown"
    return user.username or user.first_name or f"id_{user.id}"


# ---------------------------------------------------------------------------
# Silent message logger (text)
# ---------------------------------------------------------------------------

@router.message(_GROUP_FILTER, F.text)
async def _log_text_message(message: TgMessage) -> None:
    """Log metadata for every non-command text message."""
    if message.text and message.text.startswith("/"):
        return  # skip bot commands
    if message.from_user is None:
        return

    session = get_session()
    try:
        reply_to_uid: int | None = None
        if message.reply_to_message and message.reply_to_message.from_user:
            reply_to_uid = message.reply_to_message.from_user.id

        row = Message(
            message_id=message.message_id,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            username=_resolve_username(message.from_user),
            reply_to_user_id=reply_to_uid,
            timestamp=message.date.replace(tzinfo=None) if message.date else datetime.utcnow(),
            text_length=len(message.text or ""),
            has_media=False,
        )
        session.add(row)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Failed to log text message %s in %s", message.message_id, message.chat.id)
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Silent message logger (media: photo / video / document / voice / sticker)
# ---------------------------------------------------------------------------

@router.message(
    _GROUP_FILTER,
    F.content_type.in_({"photo", "video", "document", "voice", "video_note", "sticker", "animation"}),
)
async def _log_media_message(message: TgMessage) -> None:
    """Log metadata for media messages (caption length, has_media=True)."""
    if message.from_user is None:
        return

    session = get_session()
    try:
        reply_to_uid: int | None = None
        if message.reply_to_message and message.reply_to_message.from_user:
            reply_to_uid = message.reply_to_message.from_user.id

        row = Message(
            message_id=message.message_id,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            username=_resolve_username(message.from_user),
            reply_to_user_id=reply_to_uid,
            timestamp=message.date.replace(tzinfo=None) if message.date else datetime.utcnow(),
            text_length=len(message.caption or ""),
            has_media=True,
        )
        session.add(row)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Failed to log media message %s in %s", message.message_id, message.chat.id)
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# Reaction logger
# ---------------------------------------------------------------------------

@router.message_reaction()
async def _log_reaction(event: MessageReactionUpdated, bot: Bot) -> None:
    """Log reactions.  Requires the bot to be a group admin."""
    if event.user is None:
        return  # anonymous reaction – skip

    # Look up the message author from our DB
    session = get_session()
    try:
        from sqlalchemy import select

        result = await session.execute(
            select(Message.user_id).where(
                Message.message_id == event.message_id,
                Message.chat_id == event.chat.id,
            )
        )
        target_uid = result.scalar_one_or_none()
        if target_uid is None:
            return  # message not in our DB

        for rxn in event.new_reaction:
            emoji = getattr(rxn, "emoji", None) or getattr(rxn, "custom_emoji_id", "custom")
            row = Reaction(
                chat_id=event.chat.id,
                target_user_id=target_uid,
                from_user_id=event.user.id,
                reaction_type=str(emoji),
                timestamp=event.date.replace(tzinfo=None) if event.date else datetime.utcnow(),
            )
            session.add(row)
        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Failed to log reaction in %s", event.chat.id)
    finally:
        await session.close()


# ---------------------------------------------------------------------------
# /pulse command (admin-only)
# ---------------------------------------------------------------------------

@router.message(Command("pulse"), _GROUP_FILTER)
async def _pulse(message: TgMessage, bot: Bot) -> None:
    """Post a dramatic group-anomalies summary (rate-limited, admin-only)."""
    if message.from_user is None:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # ---- Admin gate -------------------------------------------------------
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        if member.status not in ("creator", "administrator"):
            await message.reply("⛔ Access denied.  `/pulse` is restricted to group admins.")
            return
    except Exception:
        logger.exception("Admin check failed for /pulse in %s", chat_id)
        await message.reply("⚠️ Could not verify admin status.")
        return

    # ---- Rate limit (1 report / 24 h) -------------------------------------
    session = get_session()
    try:
        from sqlalchemy import select

        result = await session.execute(
            select(Group).where(Group.chat_id == chat_id)
        )
        group = result.scalar_one_or_none()

        if group and group.last_report_at:
            delta = datetime.utcnow() - group.last_report_at
            if delta < timedelta(hours=24):
                remaining = timedelta(hours=24) - delta
                hours = int(remaining.total_seconds() // 3600)
                mins = int((remaining.total_seconds() % 3600) // 60)
                await message.reply(
                    f"🕐 Pulse on cooldown.  Next report available in **{hours}h {mins}m**.",
                    parse_mode="Markdown",
                )
                return
    except Exception:
        logger.exception("Rate-limit check failed for /pulse in %s", chat_id)
    finally:
        await session.close()

    # ---- Run analytics ----------------------------------------------------
    await bot.send_chat_action(chat_id, "typing")

    anomalies = await get_group_anomalies(chat_id, days=30)

    if anomalies is None:
        await message.reply(
            "📡 Insufficient telemetry.  I need more message history to analyse this group."
        )
        return

    # ---- Format output ----------------------------------------------------
    me = await bot.get_me()
    bot_uname = me.username or "VultureBot"

    top = anomalies["top_influencer"]
    ignored = anomalies.get("most_ignored")
    secret = anomalies.get("secret_dynamic")

    lines = [
        "🦅 **VULTURE PULSE // GROUP ANOMALIES**",
        "━" * 28,
        "",
        f"👑 **Top Influencer:** @{top['username']}  (Score: {top['score']:.1f}/10)",
        "",
    ]

    if ignored:
        lines.append(
            f"💀 **Most Ignored:** @{ignored['username']}  "
            f"(Neglect Rate: {ignored['neglected_rate']:.0%})"
        )
    else:
        lines.append("💀 **Most Ignored:** _No clear outlier_")
    lines.append("")

    if secret:
        lines.append(
            f"🔗 **Secret Dynamic:** @{secret['user_a']} ↔ @{secret['user_b']}\n"
            f"   Combined Weight: {secret['combined_weight']:.1f}"
        )
    else:
        lines.append("🔗 **Secret Dynamic:** _No significant mutual pair_")
    lines.append("")

    lines += [
        f"📊 **Total Subjects:** {anomalies['total_users']}  |  "
        f"**Interactions:** {anomalies['total_interactions']}",
        "",
        "⚠️ _Note: Bot requires admin privileges to capture reaction data._",
        "",
        f"_Generated by @{bot_uname}  |  /dossier in DM for personal analysis_",
    ]

    await message.reply("\n".join(lines), parse_mode="Markdown")

    # ---- Update last_report_at --------------------------------------------
    session2 = get_session()
    try:
        from sqlalchemy import update

        await session2.execute(
            update(Group).where(Group.chat_id == chat_id).values(last_report_at=datetime.utcnow())
        )
        await session2.commit()
    except Exception:
        await session2.rollback()
        logger.exception("Failed to update last_report_at for %s", chat_id)
    finally:
        await session2.close()


# ---------------------------------------------------------------------------
# Bot added / removed from group
# ---------------------------------------------------------------------------

@router.my_chat_member()
async def _track_membership(event: ChatMemberUpdated) -> None:
    """Upsert ``Group`` record when the bot is added or removed."""
    new_status = event.new_chat_member.status
    chat_id = event.chat.id
    title = event.chat.title or ""

    session = get_session()
    try:
        from sqlalchemy import select
        from sqlalchemy.dialects.sqlite import insert as sqlite_upsert

        result = await session.execute(
            select(Group).where(Group.chat_id == chat_id)
        )
        group = result.scalar_one_or_none()

        if new_status in ("member", "administrator"):
            if group is None:
                session.add(Group(chat_id=chat_id, title=title, is_active=True))
            else:
                group.title = title
                group.is_active = True
            logger.info("Bot added to group %s (%s)", title, chat_id)
        elif new_status in ("left", "kicked"):
            if group is not None:
                group.is_active = False
            logger.info("Bot removed from group %s (%s)", title, chat_id)

        await session.commit()
    except Exception:
        await session.rollback()
        logger.exception("Failed to track membership change in %s", chat_id)
    finally:
        await session.close()
