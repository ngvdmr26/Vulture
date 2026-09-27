"""Aiogram 3.x router for private (DM) commands.

* ``/start``   – Animated cyberpunk welcome with capabilities overview.
* ``/dossier`` – Generate a personal dossier card for a selected group.
"""

from __future__ import annotations

import asyncio
import logging
from html import escape

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
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

DOSSIER_PLACEHOLDER = "<code>[VULTURE] Сканирование графа связей... 💭</code>"

LOADING_PLACEHOLDER = "<code>[VULTURE] Подключение к узлу... 💭</code>"

START_MESSAGE = (
    "🦅 <b>VULTURE // ТЕРМИНАЛ СОЦИАЛЬНОЙ РАЗВЕДКИ</b>\n"
    "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    "Я незаметно отслеживаю динамику группы и строю\n"
    "психологический портрет по паттернам общения.\n\n"
    "🔍 <b>Что я делаю в группе:</b>\n"
    "• Строю социальный граф связей по ответам и реакциям\n"
    "• Вычисляю влияние участников через PageRank\n"
    "• Нахожу скрытые альянсы и игнорируемых\n"
    "• Каждое воскресенье публикую «Судный день»\n\n"
    "🛡 <b>Зачем писать мне в ЛС:</b>\n"
    "• <code>/dossier</code> — получить персональную карточку досье\n"
    "  <i>(выдаётся конфиденциально, не в общем чате)</i>\n\n"
    "📡 <b>Команды для группы:</b>\n"
    "• <code>/top</code> — рейтинг влияния участников\n"
    "• <code>/sync</code> — анализ связи между двумя людьми\n"
    "• <code>/pulse</code> — сводка аномалий (только для админов)\n\n"
    "<i>Никакие тексты сообщений не сохраняются — только метаданные.</i>\n\n"
    "Добавьте меня в группу — и я начну собирать сигналы."
)

NO_GROUPS_MESSAGE = (
    "📡 <b>[VULTURE // ОБЪЕКТ НЕ ОБНАРУЖЕН]</b>\n\n"
    "Вы пока не зафиксированы ни в одном наблюдаемом секторе.\n"
    "Добавьте бота в ваш чат или проявите активность — и система начнёт сбор телеметрии."
)

NO_ACTIVE_GROUPS_MESSAGE = (
    "📡 <b>[VULTURE // СВЯЗЬ ПОТЕРЯНА]</b>\n\n"
    "У вас нет активных групп, где бот подключён.\n"
    "Проверьте, что бот всё ещё добавлен в нужный чат."
)


# ---------------------------------------------------------------------------
# /start
# ---------------------------------------------------------------------------

@router.message(CommandStart(), F.chat.type == "private")
async def _start(message: TgMessage, bot: Bot) -> None:
    """Send the animated welcome message."""
    start_parts = message.text.split() if message.text else []
    if len(start_parts) > 1 and start_parts[1].lower() == "dossier":
        await _dossier(message, bot)
        return

    # Фаза 1: Мгновенный плейсхолдер подключения
    try:
        placeholder = await message.answer(LOADING_PLACEHOLDER, parse_mode=ParseMode.HTML)
    except TelegramAPIError:
        return

    await asyncio.sleep(1.2)

    # Фаза 2: Плавное раскрытие полного меню
    try:
        await placeholder.edit_text(START_MESSAGE, parse_mode=ParseMode.HTML)
    except TelegramAPIError:
        logger.debug("Unable to edit start placeholder for user %s", message.from_user.id if message.from_user else "?")


# ---------------------------------------------------------------------------
# /feedback (private)
# ---------------------------------------------------------------------------

@router.message(Command("feedback"), F.chat.type == "private")
async def _feedback_private(message: TgMessage, bot: Bot) -> None:
    """Handle /feedback in private chats — delegates to shared logic."""
    from handlers.feedback import handle_feedback
    await handle_feedback(message, bot)


# ---------------------------------------------------------------------------
# Group-only command stubs (friendly error in DMs)
# ---------------------------------------------------------------------------

_DM_GROUP_ONLY = (
    "ℹ️ Эта команда работает только в групповом чате.\n"
    "Добавьте бота в группу и используйте команду там."
)


@router.message(Command("top"), F.chat.type == "private")
async def _top_private(message: TgMessage) -> None:
    """Tell user that /top only works in groups."""
    try:
        await message.answer(_DM_GROUP_ONLY)
    except TelegramAPIError:
        pass


@router.message(Command("sync"), F.chat.type == "private")
async def _sync_private(message: TgMessage) -> None:
    """Tell user that /sync only works in groups."""
    try:
        await message.answer(_DM_GROUP_ONLY)
    except TelegramAPIError:
        pass


@router.message(Command("pulse"), F.chat.type == "private")
async def _pulse_private(message: TgMessage) -> None:
    """Tell user that /pulse only works in groups."""
    try:
        await message.answer(_DM_GROUP_ONLY)
    except TelegramAPIError:
        pass


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
    try:
        async with get_session() as session:
            result = await session.execute(
                select(distinct(DbMessage.chat_id)).where(DbMessage.user_id == user_id)
            )
            chat_ids: list[int] = [row for row in result.scalars().all()]

            if not chat_ids:
                await message.answer(NO_GROUPS_MESSAGE, parse_mode=ParseMode.HTML)
                return

            # Fetch group titles
            result = await session.execute(
                select(Group).where(Group.chat_id.in_(chat_ids), Group.is_active == True)  # noqa: E712
            )
            groups = list(result.scalars().all())
    except Exception:
        logger.exception("DB error in /dossier for user %s", user_id)
        try:
            await message.answer("⚠️ Временная ошибка базы данных. Повторите чуть позже.")
        except TelegramAPIError:
            pass
        return

    if not groups:
        # User has messages but the groups may have been deactivated
        try:
            await message.answer(NO_ACTIVE_GROUPS_MESSAGE, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass
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

    try:
        await message.answer(
            "📂 <b>[VULTURE // ВЫБОР СЕКТОРА]</b>\n\n"
            "Выберите группу, чтобы собрать для вас досье:",
            reply_markup=builder.as_markup(),
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        logger.debug("Failed to send group selector to user %s", user_id)


# ---------------------------------------------------------------------------
# Callback: group selection
# ---------------------------------------------------------------------------

@router.callback_query(F.data.startswith("dossier:"))
async def _on_group_selected(callback: CallbackQuery, bot: Bot) -> None:
    """Handle group selection and generate the dossier card."""
    if callback.from_user is None or callback.message is None:
        try:
            await callback.answer("⚠️ Что-то пошло не так, давайте попробуем ещё раз.")
        except TelegramAPIError:
            pass
        return

    try:
        chat_id = int(callback.data.split(":", 1)[1])  # type: ignore[union-attr]
    except (ValueError, IndexError):
        try:
            await callback.answer("⚠️ Неверный выбор, попробуйте ещё раз.")
        except TelegramAPIError:
            pass
        return

    try:
        await callback.answer()  # dismiss spinner
    except TelegramAPIError:
        pass

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
    try:
        placeholder = await target.answer(DOSSIER_PLACEHOLDER, parse_mode=ParseMode.HTML)
    except TelegramAPIError:
        return

    try:
        await bot.send_chat_action(target.chat.id, action="upload_photo")
    except TelegramAPIError:
        pass

    # 1. Metrics
    metrics = await get_user_metrics(chat_id, user_id, days=30)
    if metrics is None:
        try:
            await placeholder.edit_text(
                "⏳ <b>[КАЛИБРОВКА СЕТИ]</b>\n"
                "В базе недостаточно данных для математического расчёта.\n"
                "Система накапливает телеметрию. Повторите запрос чуть позже.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            logger.debug("Unable to update dossier placeholder for chat %s", target.chat.id)
        return

    # 2. Psychological profile
    profile = await generate_profile(metrics)

    # 3. Merge into a single dict for the card renderer
    card_data = {**metrics, **profile}

    # 4. Render PNG (offload heavy Pillow work to a thread)
    me = await bot.get_me()
    bot_username = me.username or "VultureBot"
    buf = await render_dossier(card_data, bot_username=bot_username)

    # 5. Send
    photo = BufferedInputFile(buf.read(), filename="dossier.png")
    try:
        await placeholder.delete()
    except TelegramAPIError:
        logger.debug("Unable to delete dossier placeholder for chat %s", target.chat.id)

    try:
        await target.answer_photo(
            photo,
            caption=(
                f"🦅 <b>Персональное досье @{escape(str(metrics['username']))}</b> — "
                f"<i>{escape(str(profile['rank_title']))}</i>"
            ),
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        logger.debug("Failed to send dossier card for user %s", user_id)
