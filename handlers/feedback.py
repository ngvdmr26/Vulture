"""Vulture Social Graph Intelligence — Feedback Subsystem.

Two-step feedback handler using aiogram FSM with animated cooldown.
Captures bug reports, anomalies, and feature requests.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import ForceReply, Message as TgMessage

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
_TIMER_INTERVAL_SEC = 3  # Безопасный интервал обновления без риска FloodWait

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
        "📡 <b>[VULTURE // КАНАЛ СВЯЗИ]</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Ответьте на это сообщение (Reply) вашим текстом: баг-репорт, найденная аномалия или идея.\n\n"
        "<i>Для отмены передачи используйте команду</i> /cancel"
    )

    try:
        await message.reply(
            prompt_text,
            parse_mode=ParseMode.HTML,
            reply_markup=ForceReply(selective=True),
        )
    except TelegramAPIError as exc:
        logger.error("[FEEDBACK] Failed to send prompt: %s", exc)
        await state.clear()


@router.message(Command("cancel"), FeedbackStates.waiting_for_text)
async def _feedback_cancel(message: TgMessage, state: FSMContext) -> None:
    """Cancel the active feedback transmission."""
    await state.clear()
    try:
        await message.reply(
            "🛑 <b>[VULTURE // СЕССИЯ ПРЕРВАНА]</b>\nПередача данных отменена.",
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        pass


@router.message(FeedbackStates.waiting_for_text, F.text)
async def _feedback_receive_text(message: TgMessage, state: FSMContext) -> None:
    """Step 2: Payload capture and processing."""
    logger.info("[FEEDBACK] Text received from user %s",
                message.from_user.id if message.from_user else "?")
    await state.clear()

    if message.from_user is None:
        return

    text = (message.text or "").strip()
    if not text:
        try:
            await message.reply(
                "⚠️ <b>[VULTURE // ПУСТОЙ ПАКЕТ]</b>\n"
                "Сообщение пустое. Повторите отправку: <code>/feedback &lt;сообщение&gt;</code>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    await _process_feedback_text(message, text)


# ---------------------------------------------------------------------------
# Core Delivery Logic & Animated Rate-Limiter
# ---------------------------------------------------------------------------

async def _process_feedback_text(message: TgMessage, text: str) -> None:
    """Validate cooldown with dynamic UI, format report, and route payload."""
    if message.from_user is None:
        return

    user_id = message.from_user.id
    bot: Bot = message.bot  # type: ignore[assignment]

    # Проверка кулдауна
    now = time.monotonic()
    last_time = _feedback_cooldown.get(user_id, 0.0)
    elapsed = now - last_time

    if elapsed < _FEEDBACK_COOLDOWN_SEC:
        remaining = int(_FEEDBACK_COOLDOWN_SEC - elapsed)

        # Стартовая отрисовка прогресс-бара
        progress = int((1 - remaining / _FEEDBACK_COOLDOWN_SEC) * 10)
        bar = "■" * progress + "□" * (10 - progress)

        try:
            sent_msg = await message.reply(
                f"⏳ <b>[VULTURE // ОХЛАЖДЕНИЕ КАНАЛА: {remaining} СЕК]</b>\n"
                f"<code>[{bar}]</code> Буферизация канала связи...",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            return

        # Динамический цикл обновления плашки
        while remaining > 0:
            await asyncio.sleep(min(_TIMER_INTERVAL_SEC, remaining))
            remaining -= _TIMER_INTERVAL_SEC

            if remaining <= 0:
                try:
                    await sent_msg.edit_text(
                        "🟢 <b>[VULTURE // КАНАЛ ГОТОВ]</b>\n"
                        "Буферизация завершена. Повторите отправку фидбека.",
                        parse_mode=ParseMode.HTML,
                    )
                except TelegramAPIError:
                    pass
                break

            progress = int((1 - remaining / _FEEDBACK_COOLDOWN_SEC) * 10)
            bar = "■" * progress + "□" * (10 - progress)

            try:
                await sent_msg.edit_text(
                    f"⏳ <b>[VULTURE // ОХЛАЖДЕНИЕ КАНАЛА: {remaining} СЕК]</b>\n"
                    f"<code>[{bar}]</code> Буферизация канала связи...",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramAPIError:
                # Если сообщение удалили или сеть сбойнула — выходим из цикла
                break

        return

    # Запоминаем время отправки
    _feedback_cooldown[user_id] = time.monotonic()

    settings = get_settings()
    if not settings.DEVELOPER_ID:
        logger.warning("[FEEDBACK] DEVELOPER_ID missing in configuration.")
        try:
            await message.reply(
                "⚠️ <b>[VULTURE // СЕРВИС НЕДОСТУПЕН]</b>\n"
                "Модуль связи с разработчиком временно деактивирован на сервере.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    username = f"@{message.from_user.username}" if message.from_user.username else (message.from_user.first_name or "Unknown")
    chat_title = message.chat.title if message.chat.type != "private" else "Direct Message"
    chat_id = message.chat.id

    report = (
        "📥 <b>[VULTURE FEEDBACK INCOMING]</b>\n"
        "────────────────────────\n"
        f"👤 <b>Источник:</b> {html.escape(username)} (<code>{user_id}</code>)\n"
        f"📍 <b>Узел (чат):</b> {html.escape(chat_title)} (<code>{chat_id}</code>)\n"
        "────────────────────────\n"
        f"💬 <b>Содержимое:</b>\n{html.escape(text)}"
    )

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
                "❌ <b>[VULTURE // СБОЙ МАРШРУТИЗАЦИИ]</b>\n"
                "Не удалось доставить пакет из-за сетевой ошибки. Попробуйте позже.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    try:
        await message.reply(
            "📡 <b>[VULTURE // ПАКЕТ ДОСТАВЛЕН]</b>\n"
            "Данные успешно переданы на терминал разработчика. Спасибо за содействие!",
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError as exc:
        logger.error("[FEEDBACK] Confirmation failed: %s", exc)