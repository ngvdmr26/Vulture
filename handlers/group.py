"""Aiogram 3.x router for group-chat events.

Commands: /pulse (admin-only), /sync, /top, /dossier (redirect to DM).
Telemetry: silent message & reaction logging.
Auto-calibration: fires a one-time notification when the graph crosses
the MIN_INTERACTIONS threshold for the first time.
"""

from __future__ import annotations

import asyncio
import html
import logging
from datetime import datetime, timezone

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    ChatMemberUpdated,
    Message as TgMessage,
    MessageReactionUpdated,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from database import Group, Message, Reaction, get_session
from engine.card_generator import render_sync_card
from engine.graph import (
    _MIN_INTERACTIONS,
    get_graph_edge_count,
    get_group_anomalies,
    get_group_leaderboard,
    get_pair_metrics,
    get_top_active_users,
)
from services.terminal_ui import (
    safe_delete,
    send_animated_terminal,
    send_card_status,
    send_progress_bar,
)

logger = logging.getLogger(__name__)
router = Router(name="group")

_GROUP_FILTER = F.chat.type.in_({"group", "supergroup"})

CALIBRATION_INITIALIZED_MESSAGE = (
    "🟢 <b>[VULTURE // НЕЙРОСЕТЬ АКТИВИРОВАНА]</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Граф социальных связей набрал достаточно данных. Теперь доступны все команды:\n\n"
    "• <code>/top</code> — иерархия влияния в чате\n"
    "• <code>/sync</code> — анализ связи между двумя участниками\n"
    "• <code>/dossier</code> — личное психологическое досье (в ЛС)\n"
    "• <code>/pulse</code> — сводка аномалий (только для администраторов)\n\n"
    "<i>Данные обновляются в реальном времени. Еженедельный разбор по воскресеньям.</i>"
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
# Progress bar helper (tasks 2 + 6)
# ---------------------------------------------------------------------------

async def _show_calibration_progress(message: TgMessage, chat_id: int) -> bool:
    """Show progress bar if under threshold.  Returns True if shown."""
    edge_count = await get_graph_edge_count(chat_id, days=30)
    if edge_count >= _MIN_INTERACTIONS:
        return False
    await send_progress_bar(
        message,
        current=edge_count,
        total=_MIN_INTERACTIONS,
    )
    return True


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

    # Show progress bar if not enough data
    if await _show_calibration_progress(message, chat_id):
        return

    status_msg = await send_animated_terminal(
        message,
        final_text="<code>[SYS] Дешифровка топологии группы... ⏳</code>",
        operation_name="АНАЛИЗ АНОМАЛИЙ",
    )

    try:
        await bot.send_chat_action(chat_id, action="typing")
    except TelegramAPIError:
        pass

    anomalies = await get_group_anomalies(chat_id, days=30)
    if not anomalies or not anomalies.get("top_influencer"):
        if status_msg:
            try:
                await status_msg.edit_text(
                    "📉 <b>[КАЛИБРОВКА СЕТИ]</b>\n"
                    "Недостаточно данных для анализа. Граф формируется в реальном времени.\n"
                    "Первые метрики станут доступны по мере активности чата.",
                    parse_mode=ParseMode.HTML,
                )
            except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError):
                pass
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

    final_text = "\n".join(lines)
    if status_msg:
        try:
            await status_msg.edit_text(final_text, parse_mode=ParseMode.HTML)
        except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError):
            logger.debug("Unable to update pulse status in chat %s", chat_id)
    else:
        try:
            await message.reply(final_text, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass


@router.message(Command("top"), _GROUP_FILTER)
async def _top(message: TgMessage, bot: Bot) -> None:
    """Show group influence leaderboard."""
    chat_id = message.chat.id

    # Show progress bar if not enough data
    if await _show_calibration_progress(message, chat_id):
        return

    status_msg = await send_animated_terminal(
        message,
        final_text="<code>[SYS] Дешифровка топологии группы... ⏳</code>",
        operation_name="РАСЧЁТ ИЕРАРХИИ",
    )

    try:
        await bot.send_chat_action(chat_id, action="typing")
    except TelegramAPIError:
        pass

    leaderboard = await get_group_leaderboard(chat_id, limit=7)
    if not leaderboard:
        calib_text = (
            "📉 <b>[КАЛИБРОВКА СЕТИ]</b>\n"
            "Недостаточно данных для анализа. Граф формируется в реальном времени.\n"
            "Первые метрики станут доступны по мере активности чата."
        )
        if status_msg:
            try:
                await status_msg.edit_text(calib_text, parse_mode=ParseMode.HTML)
            except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError):
                pass
        else:
            try:
                await message.reply(calib_text, parse_mode=ParseMode.HTML)
            except TelegramAPIError:
                pass
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

    final_text = "\n".join(lines)
    if status_msg:
        try:
            await status_msg.edit_text(final_text, parse_mode=ParseMode.HTML)
        except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError):
            logger.debug("Unable to update top status in chat %s", chat_id)
    else:
        try:
            await message.reply(final_text, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass


# ---------------------------------------------------------------------------
# /sync — with inline keyboard picker (task 1)
# ---------------------------------------------------------------------------

@router.message(Command("sync"), _GROUP_FILTER)
async def _sync(message: TgMessage, bot: Bot) -> None:
    """Pair analysis between invoker and replied user or tagged username."""
    if message.from_user is None:
        return

    chat_id = message.chat.id
    user_a_id = message.from_user.id
    user_b_id: int | None = None

    # Вариант 1: Реплай на сообщение другого участника
    if message.reply_to_message and message.reply_to_message.from_user:
        user_b_id = message.reply_to_message.from_user.id

    # Вариант 2: /sync @username
    if user_b_id is None:
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

    # Вариант 3: Без аргументов — показываем инлайн-клавиатуру
    if user_b_id is None or user_b_id == user_a_id:
        await _sync_show_picker(message, chat_id, user_a_id)
        return

    await _sync_run(message, bot, chat_id, user_a_id, user_b_id)


async def _sync_show_picker(message: TgMessage, chat_id: int, caller_id: int) -> None:
    """Show inline keyboard with top-6 active users for /sync."""
    # Show progress bar first if not enough data
    edge_count = await get_graph_edge_count(chat_id, days=30)
    if edge_count < _MIN_INTERACTIONS:
        await send_progress_bar(message, current=edge_count, total=_MIN_INTERACTIONS)
        return

    top_users = await get_top_active_users(
        chat_id=chat_id, exclude_user_id=caller_id, limit=6, days=7
    )

    if not top_users:
        try:
            await message.reply(
                "⚠️ <b>[ОШИБКА МАРШРУТА]</b>\n"
                "Недостаточно участников для выбора. Ответьте командой "
                "<code>/sync</code> на сообщение участника или напишите "
                "<code>/sync @username</code>.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    builder = InlineKeyboardBuilder()
    for u in top_users:
        uname = u["username"]
        builder.button(
            text=f"@{uname}",
            callback_data=f"sync:{caller_id}:{u['user_id']}",
        )
    builder.adjust(2)  # 2 columns = 2×3 grid

    try:
        await message.reply(
            "🔭 <b>[VULTURE // СКАНЕР СВЯЗЕЙ]</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Выберите участника для анализа парной связи:\n\n"
            "<i>Или: ответьте /sync на сообщение участника · /sync @username</i>",
            parse_mode=ParseMode.HTML,
            reply_markup=builder.as_markup(),
        )
    except TelegramAPIError:
        logger.debug("Failed to send sync picker in chat %s", chat_id)


@router.callback_query(F.data.startswith("sync:"))
async def _sync_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Handle sync target selection from the inline keyboard."""
    if callback.from_user is None or callback.message is None:
        try:
            await callback.answer("⚠️ Что-то пошло не так.", show_alert=True)
        except TelegramAPIError:
            pass
        return

    try:
        _, caller_id_str, target_id_str = callback.data.split(":")  # type: ignore[union-attr]
        caller_id = int(caller_id_str)
        target_id = int(target_id_str)
    except (ValueError, AttributeError):
        try:
            await callback.answer("⚠️ Неверный формат данных.", show_alert=True)
        except TelegramAPIError:
            pass
        return

    # Only the original caller may trigger the scan
    if callback.from_user.id != caller_id:
        try:
            await callback.answer(
                "Это сканирование запущено другим пользователем.",
                show_alert=True,
            )
        except TelegramAPIError:
            pass
        return

    try:
        await callback.answer()
    except TelegramAPIError:
        pass

    # Remove the picker keyboard
    try:
        await callback.message.delete()
    except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError):
        pass

    await _sync_run(callback.message, bot, callback.message.chat.id, caller_id, target_id)


async def _sync_run(
    message: TgMessage,
    bot: Bot,
    chat_id: int,
    user_a_id: int,
    user_b_id: int,
) -> None:
    """Build and send the sync card."""
    status_msg = await send_card_status(message, card_label="ГЕНЕРАЦИЯ КАРТОЧКИ СВЯЗЕЙ")

    try:
        await bot.send_chat_action(chat_id, "upload_photo")
    except TelegramAPIError:
        pass

    pair_data = await get_pair_metrics(chat_id, user_a_id, user_b_id)
    if not pair_data:
        calib_text = (
            "📉 <b>[КАЛИБРОВКА СЕТИ]</b>\n"
            "Недостаточно данных для анализа. Граф формируется в реальном времени.\n"
            "Первые метрики станут доступны по мере активности чата."
        )
        await safe_delete(status_msg)
        try:
            await message.reply(calib_text, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass
        return

    me = await bot.get_me()
    buf = await render_sync_card(pair_data, bot_username=me.username or "VultureBot")
    photo_file = BufferedInputFile(buf.getvalue(), filename="sync_card.png")

    await safe_delete(status_msg)

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


async def _check_and_fire_calibration(bot: Bot, chat_id: int) -> None:
    """Check if the graph just crossed the init threshold; fire notification once."""
    try:
        async with get_session() as session:
            res = await session.execute(select(Group).where(Group.chat_id == chat_id))
            grp = res.scalar_one_or_none()
            if grp is None or grp.is_initialized:
                return

        edge_count = await get_graph_edge_count(chat_id, days=30)
        if edge_count < _MIN_INTERACTIONS:
            return

        # Cross the threshold for the first time → mark initialized and notify
        async with get_session() as session:
            res = await session.execute(select(Group).where(Group.chat_id == chat_id))
            grp = res.scalar_one_or_none()
            if grp is None or grp.is_initialized:
                return  # race-condition guard
            grp.is_initialized = True
            await session.commit()

        await bot.send_message(
            chat_id,
            CALIBRATION_INITIALIZED_MESSAGE,
            parse_mode=ParseMode.HTML,
        )
        logger.info("Calibration complete — fired notification for chat %s", chat_id)
    except Exception:
        logger.debug("_check_and_fire_calibration failed for chat %s", chat_id)


@router.message(_GROUP_FILTER, F.text)
async def _log_text_message(message: TgMessage, bot: Bot) -> None:
    """Log metadata for every non-command text message."""
    if message.text and message.text.startswith("/"):
        return
    if message.from_user is None:
        return

    has_reply = bool(
        message.reply_to_message and message.reply_to_message.from_user
    )

    try:
        async with get_session() as session:
            await _ensure_group_active(session, message.chat.id, message.chat.title or "")

            reply_to_uid: int | None = None
            if has_reply:
                reply_to_uid = message.reply_to_message.from_user.id  # type: ignore[union-attr]

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
        return

    # Check calibration threshold asynchronously (fire-and-forget, non-blocking)
    if has_reply:
        asyncio.create_task(_check_and_fire_calibration(bot, message.chat.id))


@router.message(
    _GROUP_FILTER,
    F.content_type.in_({"photo", "video", "document", "voice", "video_note", "sticker", "animation"}),
)
async def _log_media_message(message: TgMessage, bot: Bot) -> None:
    """Log metadata for media messages."""
    if message.from_user is None:
        return

    has_reply = bool(
        message.reply_to_message and message.reply_to_message.from_user
    )

    try:
        async with get_session() as session:
            await _ensure_group_active(session, message.chat.id, message.chat.title or "")

            reply_to_uid: int | None = None
            if has_reply:
                reply_to_uid = message.reply_to_message.from_user.id  # type: ignore[union-attr]

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
        return

    if has_reply:
        asyncio.create_task(_check_and_fire_calibration(bot, message.chat.id))


@router.message_reaction()
async def _log_reaction(event: MessageReactionUpdated, bot: Bot) -> None:
    """Log reactions and check calibration threshold."""
    if event.user is None:
        return

    saved = False
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
            saved = True
    except Exception:
        logger.debug("Unable to persist reaction telemetry for message %s", event.message_id)

    if saved:
        asyncio.create_task(_check_and_fire_calibration(bot, event.chat.id))


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