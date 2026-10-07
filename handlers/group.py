"""Aiogram 3.x router for group-chat events.

Commands: /pulse (admin-only), /sync, /top, /dossier (redirect to DM).
Telemetry: silent message & reaction logging.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    ChatMemberUpdated,
    Message as TgMessage,
    MessageReactionUpdated,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from database import Group, Message, Reaction, get_session
from engine.card_generator import render_sync_card
from engine.graph import (
    get_group_anomalies,
    get_group_leaderboard,
    get_pair_metrics,
)

logger = logging.getLogger(__name__)
router = Router(name="group")

_GROUP_FILTER = F.chat.type.in_({"group", "supergroup"})

CALIBRATION_MESSAGE = (
    "📉 <b>[КАЛИБРОВКА СЕТИ]</b>\n"
    "Недостаточно данных для анализа. Граф формируется в реальном времени.\n"
    "Первые метрики станут доступны по мере активности чата."
)

WELCOME_MESSAGE = (
    "📊 <b>Vulture запущен в чате: режим сбора связей</b>\n\n"
    "Всем привет! Я аналитический бот. Моя цель — через 7 дней построить наглядную "
    "карту общения чата: показать, кто с кем чаще всего ведет диалоги, выделить самые "
    "активные связки и сформировать недельный рейтинг.\n\n"
    "⏳ <b>Почему команды пока не работают на полную?</b>\n"
    "База данных сейчас пуста. Бот начинает считать взаимодействия с нуля. Первые "
    "осмысленные отчеты (<code>/top</code>, <code>/sync</code>) появятся, когда "
    "накопится хотя бы несколько дней живой переписки. Сейчас тыкать команды нет "
    "смысла — просто общайтесь в привычном ритме.\n\n"
    "🛡 <b>Приватность:</b>\n"
    "Текст сообщений не сохраняется и не читается. Учитываются только факты "
    "взаимодействий (кто кому ответил и поставил реакцию).\n\n"
    "<i>(Бот на тесте. Админы могут удалить его в любой момент, если присутствие нежелательно)</i>"
)

GROUP_DOSSIER_MESSAGE = (
    "🔒 <b>VULTURE // ПРИВАТНЫЙ ПРОТОКОЛ</b>\n\n"
    "Персональное досье выдаётся строго конфиденциально в ЛС."
)

ACCESS_DENIED_MESSAGE = (
    "⛔ <b>[ОТКАЗ В ДОСТУПЕ]</b>\n"
    "Модуль телеметрии аномалий доступен только узлам с правами администратора группы."
)


def _resolve_username(user) -> str:
    """Return the best available display name, never None."""
    if user is None:
        return "unknown"
    return user.username or user.first_name or f"id_{user.id}"


# ---------------------------------------------------------------------------
# Admin check helper
# ---------------------------------------------------------------------------

async def _is_group_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
    """Return True if *user_id* is creator or administrator of *chat_id*."""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in ("creator", "administrator")
    except TelegramAPIError:
        logger.debug("Failed to check admin status for user %s in chat %s", user_id, chat_id)
        return False


# ---------------------------------------------------------------------------
# 1. COMMANDS
# ---------------------------------------------------------------------------

@router.message(Command("dossier"), _GROUP_FILTER)
async def _group_dossier(message: TgMessage, bot: Bot) -> None:
    """Direct the user to a private dossier deep link."""
    try:
        me = await bot.get_me()
        builder = InlineKeyboardBuilder()
        builder.button(
            text="🗂 Открыть досье в ЛС",
            url=f"https://t.me/{me.username}?start=dossier",
        )
        await message.reply(
            GROUP_DOSSIER_MESSAGE,
            parse_mode=ParseMode.HTML,
            reply_markup=builder.as_markup(),
        )
    except TelegramAPIError:
        logger.debug("Failed to send dossier redirect in chat %s", message.chat.id)


@router.message(Command("pulse"), _GROUP_FILTER)
async def _pulse(message: TgMessage, bot: Bot) -> None:
    """Post group-anomalies summary (admin-only)."""
    if message.from_user is None:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id

    # --- Admin-Only gate ---
    if not await _is_group_admin(bot, chat_id, user_id):
        try:
            await message.reply(ACCESS_DENIED_MESSAGE, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass
        return

    try:
        status_msg = await message.reply(
            "<code>[SYS] Дешифровка топологии группы... ⏳</code>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        return

    try:
        await bot.send_chat_action(chat_id, action="typing")
    except TelegramAPIError:
        pass

    anomalies = await get_group_anomalies(chat_id, days=30)
    if not anomalies or not anomalies.get("top_influencer"):
        try:
            await status_msg.edit_text(CALIBRATION_MESSAGE, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            logger.debug("Unable to update pulse status in chat %s", chat_id)
        return

    top = anomalies["top_influencer"]
    ignored = anomalies.get("most_ignored")
    secret = anomalies.get("secret_dynamic")

    lines = [
        "🦅 <b>VULTURE PULSE // АНОМАЛИИ ГРУППЫ</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
        f"👑 <b>Главный влияющий:</b> @{html.escape(top['username'])} (Оценка: {top['score']:.1f}/10)",
        "",
    ]
    if ignored:
        lines.append(f"💀 <b>Самый игнорируемый:</b> @{html.escape(ignored['username'])} (Игнор: {ignored['neglected_rate']:.0%})")
    else:
        lines.append("💀 <b>Самый игнорируемый:</b> <i>Нет явного выброса</i>")
    lines.append("")

    if secret:
        lines.append(f"🔗 <b>Тайная связь:</b> @{html.escape(secret['user_a'])} ↔ @{html.escape(secret['user_b'])}\n   Вес: {secret['combined_weight']:.1f}")
    else:
        lines.append("🔗 <b>Тайная связь:</b> <i>Нет выраженной пары</i>")
    lines.append("")

    lines += [
        f"📊 <b>Участников:</b> {anomalies['total_users']} | <b>Взаимодействий:</b> {anomalies['total_interactions']}",
        "",
        "<i>Используйте /sync реплаем на сообщение, чтобы проверить связь.</i>",
    ]
    try:
        await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except TelegramAPIError:
        logger.debug("Unable to update pulse status in chat %s", chat_id)


@router.message(Command("top"), _GROUP_FILTER)
async def _top(message: TgMessage, bot: Bot) -> None:
    """Show group influence leaderboard."""
    try:
        status_msg = await message.reply(
            "<code>[SYS] Дешифровка топологии группы... ⏳</code>",
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        return

    try:
        await bot.send_chat_action(message.chat.id, action="typing")
    except TelegramAPIError:
        pass

    leaderboard = await get_group_leaderboard(message.chat.id, limit=7)
    if not leaderboard:
        try:
            await status_msg.edit_text(CALIBRATION_MESSAGE, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            logger.debug("Unable to update top status in chat %s", message.chat.id)
        return

    medals = ["🥇", "🥈", "🥉", "4️⃣", "5️⃣", "6️⃣", "7️⃣"]
    lines = [
        "🏆 <b>VULTURE // ИЕРАРХИЯ ВЛИЯНИЯ В ЧАТЕ</b>",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "",
    ]
    for idx, user in enumerate(leaderboard):
        icon = medals[idx] if idx < len(medals) else "▫️"
        lines.append(
            f"{icon} <b>@{html.escape(user['username'])}</b> — {user['score']} баллов\n"
            f"   <i>Статус: {user['status']}</i>"
        )

    try:
        await status_msg.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)
    except TelegramAPIError:
        logger.debug("Unable to update top status in chat %s", message.chat.id)


@router.message(Command("sync"), _GROUP_FILTER)
async def _sync(message: TgMessage, bot: Bot) -> None:
    """Pair analysis between invoker and replied user or tagged username."""
    if message.from_user is None:
        return

    chat_id = message.chat.id
    user_a_id = message.from_user.id
    user_b_id: int | None = None

    # Вариант 1: Команда вызвана реплаем на сообщение другого юзера
    if message.reply_to_message and message.reply_to_message.from_user:
        user_b_id = message.reply_to_message.from_user.id
    else:
        # Вариант 2: Передан тег /sync @username
        parts = message.text.split() if message.text else []
        if len(parts) > 1 and parts[1].startswith("@"):
            raw_tag = parts[1].lstrip("@")
            try:
                async with get_session() as session:
                    res = await session.execute(
                        select(Message.user_id)
                        .where(Message.chat_id == chat_id, Message.username == raw_tag)
                        .limit(1)
                    )
                    user_b_id = res.scalar_one_or_none()
            except Exception:
                logger.debug("DB lookup failed for username %s in /sync", raw_tag)

    if not user_b_id or user_b_id == user_a_id:
        try:
            await message.reply(
                "⚠️ <b>Как использовать /sync:</b>\n"
                "Ответьте командой <code>/sync</code> на сообщение участника, "
                "либо напишите <code>/sync @username</code>.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    try:
        await bot.send_chat_action(chat_id, "upload_photo")
    except TelegramAPIError:
        pass

    pair_data = await get_pair_metrics(chat_id, user_a_id, user_b_id)
    if not pair_data:
        try:
            await message.reply(CALIBRATION_MESSAGE, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass
        return

    me = await bot.get_me()
    buf = await render_sync_card(pair_data, bot_username=me.username or "VultureBot")
    photo_file = BufferedInputFile(buf.getvalue(), filename="sync_card.png")
    try:
        await message.reply_photo(photo_file)
    except TelegramAPIError:
        logger.debug("Failed to send sync card in chat %s", chat_id)


# ---------------------------------------------------------------------------
# 2. SILENT LOGGERS
# ---------------------------------------------------------------------------

async def _ensure_group_active(session, chat_id: int, title: str = "") -> None:
    """Ensure the group record exists and is marked active in the database."""
    res = await session.execute(select(Group).where(Group.chat_id == chat_id))
    grp = res.scalar_one_or_none()
    if grp is None:
        session.add(Group(chat_id=chat_id, title=title, is_active=True))
        await session.flush()
    elif not grp.is_active:
        grp.is_active = True
        if title:
            grp.title = title


@router.message(_GROUP_FILTER, F.text)
async def _log_text_message(message: TgMessage) -> None:
    """Log metadata for every non-command text message."""
    if message.text and message.text.startswith("/"):
        return
    if message.from_user is None:
        return

    try:
        async with get_session() as session:
            await _ensure_group_active(session, message.chat.id, message.chat.title or "")

            reply_to_uid: int | None = None
            if message.reply_to_message and message.reply_to_message.from_user:
                reply_to_uid = message.reply_to_message.from_user.id

            row = Message(
                message_id=message.message_id,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                username=_resolve_username(message.from_user),
                reply_to_user_id=reply_to_uid,
                timestamp=message.date.replace(tzinfo=None) if message.date else datetime.now(timezone.utc).replace(tzinfo=None),
                text_length=len(message.text or ""),
                has_media=False,
            )
            session.add(row)
            await session.commit()
    except Exception:
        logger.debug("Unable to persist text telemetry for message %s", message.message_id)


@router.message(
    _GROUP_FILTER,
    F.content_type.in_({"photo", "video", "document", "voice", "video_note", "sticker", "animation"}),
)
async def _log_media_message(message: TgMessage) -> None:
    """Log metadata for media messages."""
    if message.from_user is None:
        return

    try:
        async with get_session() as session:
            await _ensure_group_active(session, message.chat.id, message.chat.title or "")

            reply_to_uid: int | None = None
            if message.reply_to_message and message.reply_to_message.from_user:
                reply_to_uid = message.reply_to_message.from_user.id

            row = Message(
                message_id=message.message_id,
                chat_id=message.chat.id,
                user_id=message.from_user.id,
                username=_resolve_username(message.from_user),
                reply_to_user_id=reply_to_uid,
                timestamp=message.date.replace(tzinfo=None) if message.date else datetime.now(timezone.utc).replace(tzinfo=None),
                text_length=len(message.caption or ""),
                has_media=True,
            )
            session.add(row)
            await session.commit()
    except Exception:
        logger.debug("Unable to persist media telemetry for message %s", message.message_id)


@router.message_reaction()
async def _log_reaction(event: MessageReactionUpdated) -> None:
    """Log reactions."""
    if event.user is None:
        return

    try:
        async with get_session() as session:
            res = await session.execute(
                select(Message.user_id).where(
                    Message.message_id == event.message_id,
                    Message.chat_id == event.chat.id,
                )
            )
            target_uid = res.scalar_one_or_none()
            if target_uid is None:
                return

            for rxn in event.new_reaction:
                emoji = getattr(rxn, "emoji", None) or getattr(rxn, "custom_emoji_id", "custom")
                row = Reaction(
                    chat_id=event.chat.id,
                    target_user_id=target_uid,
                    from_user_id=event.user.id,
                    reaction_type=str(emoji),
                    timestamp=event.date.replace(tzinfo=None) if event.date else datetime.now(timezone.utc).replace(tzinfo=None),
                )
                session.add(row)
            await session.commit()
    except Exception:
        logger.debug("Unable to persist reaction telemetry for message %s", event.message_id)


@router.my_chat_member()
async def _track_membership(event: ChatMemberUpdated, bot: Bot) -> None:
    """Upsert Group record when bot is added or removed; send welcome on join."""
    new_status = event.new_chat_member.status
    chat_id = event.chat.id
    title = event.chat.title or ""

    try:
        async with get_session() as session:
            res = await session.execute(select(Group).where(Group.chat_id == chat_id))
            group = res.scalar_one_or_none()

            if new_status in ("member", "administrator"):
                if group is None:
                    session.add(Group(chat_id=chat_id, title=title, is_active=True))
                else:
                    group.title = title
                    group.is_active = True
            elif new_status in ("left", "kicked"):
                if group is not None:
                    group.is_active = False

            await session.commit()
    except Exception:
        logger.exception("Failed to update group membership for chat %s", chat_id)

    # Send welcome only on first addition
    if event.old_chat_member.status not in ("member", "administrator") and new_status in ("member", "administrator"):
        try:
            await bot.send_message(chat_id, WELCOME_MESSAGE, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            logger.debug("Failed to send welcome message to chat %s", chat_id)