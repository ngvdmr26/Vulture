"""Vulture Social Graph Intelligence — Feedback Subsystem.

Two-step feedback handler using aiogram FSM.
Captures bug reports, anomalies, and feature requests.
"""

from __future__ import annotations

import html
import logging
import time

from aiogram import Bot, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message as TgMessage

from config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FSM States
# ---------------------------------------------------------------------------

class FeedbackStates(StatesGroup):
    waiting_for_text = State()


# ---------------------------------------------------------------------------
# Rate-limit (in-memory)
# ---------------------------------------------------------------------------

_feedback_cooldown: dict[int, float] = {}
_FEEDBACK_COOLDOWN_SEC = 30

# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------

router = Router(name="feedback")


@router.message(Command("feedback"))
async def _feedback_start(message: TgMessage, state: FSMContext) -> None:
    """Step 1: Initiation of transmission channel."""
    if message.from_user is None:
        return

    logger.info("[FEEDBACK] Init by user %s in chat %s", message.from_user.id, message.chat.id)

    # Однострочный ввод: /feedback <текст>
    inline_text = (message.text or "").partition(" ")[2].strip()
    if inline_text:
        await _process_feedback_text(message, inline_text)
        return

    # Двухэтапный ввод через FSM
    await state.set_state(FeedbackStates.waiting_for_text)
    
    prompt_text = (
        "📡 <b>Канал связи с разработчиком</b>\n\n"
        "Отправьте следующим сообщением отчет об аномалии, баг-репорт или предложение.\n"
        "Для отмены передачи используйте команду /cancel."
    )
    
    try:
        await message.reply(prompt_text, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.error("[FEEDBACK] Failed to send prompt: %s", exc)
        await state.clear()


@router.message(Command("cancel"), FeedbackStates.waiting_for_text)
async def _feedback_cancel(message: TgMessage, state: FSMContext) -> None:
    """Cancel the active feedback transmission."""
    await state.clear()
    try:
        await message.reply("🛑 Сессия передачи данных отменена.", parse_mode=ParseMode.HTML)
    except TelegramAPIError:
        pass


@router.message(FeedbackStates.waiting_for_text)
async def _feedback_receive_text(message: TgMessage, state: FSMContext) -> None:
    """Step 2: Payload capture and processing."""
    # Очищаем состояние сразу, чтобы освободить пайплайн
    await state.clear()

    if message.from_user is None:
        return

    text = (message.text or "").strip()
    if not text:
        try:
            await message.reply(
                "⚠️ Пакет пуст. Повторите отправку: <code>/feedback &lt;сообщение&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    await _process_feedback_text(message, text)


# ---------------------------------------------------------------------------
# Core Delivery Logic
# ---------------------------------------------------------------------------

async def _process_feedback_text(message: TgMessage, text: str) -> None:
    """Validate cooldown, format report, and route payload to DEVELOPER_ID."""
    if message.from_user is None:
        return

    user_id = message.from_user.id
    bot: Bot = message.bot  # type: ignore[assignment]

    # Защита от флуда
    now = time.monotonic()
    last_time = _feedback_cooldown.get(user_id, 0.0)
    if now - last_time < _FEEDBACK_COOLDOWN_SEC:
        remaining = int(_FEEDBACK_COOLDOWN_SEC - (now - last_time))
        try:
            await message.reply(
                f"⏳ Лимит частоты запросов. Повторная отправка доступна через {remaining} сек.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    _feedback_cooldown[user_id] = now

    settings = get_settings()
    if not settings.DEVELOPER_ID:
        logger.warning("[FEEDBACK] DEVELOPER_ID missing in configuration.")
        try:
            await message.reply("⚠️ Модуль связи временно деактивирован на сервере.", parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass
        return

    username = f"@{message.from_user.username}" if message.from_user.username else (message.from_user.first_name or "Unknown")
    chat_title = message.chat.title if message.chat.type != "private" else "Direct Message"
    chat_id = message.chat.id

    # Пакет для отправки разработчику
    report = (
        "📥 <b>[VULTURE FEEDBACK INCOMING]</b>\n"
        "────────────────────────\n"
        f"👤 <b>Источник:</b> {html.escape(username)} (<code>{user_id}</code>)\n"
        f"📍 <b>Узел (чат):</b> {html.escape(chat_title)} (<code>{chat_id}</code>)\n"
        "────────────────────────\n"
        f"💬 <b>Содержимое:</b>\n{html.escape(text)}"
    )

    # Приводим к int на случай, если из .env прочиталась строка
    dev_target_id = int(settings.DEVELOPER_ID)

    try:
        await bot.send_message(
            chat_id=dev_target_id,
            text=report,
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError as exc:
        logger.error("[FEEDBACK] Delivery failed to dev (%s): %s", dev_target_id, exc)
        try:
            await message.reply(
                "❌ Ошибка маршрутизации пакета. Канал разработчика временно недоступен.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    try:
        await message.reply(
            "📡 <b>Пакет доставлен.</b> Данные переданы на центральный узел разработчика.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError as exc:
        logger.error("[FEEDBACK] Confirmation failed: %s", exc)