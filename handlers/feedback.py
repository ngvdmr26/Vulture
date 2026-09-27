"""Two-step /feedback handler using aiogram FSM.

Flow:
  1. User sends /feedback  → bot asks for the text.
  2. User sends a text message → bot forwards it to DEVELOPER_ID.

Works in both group and private chats.  Extracted into its own
module to avoid circular imports between the group and private routers.
"""

from __future__ import annotations

import html
import logging
import time

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message as TgMessage

from config import get_settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# FSM states
# ---------------------------------------------------------------------------

class FeedbackStates(StatesGroup):
    waiting_for_text = State()


# ---------------------------------------------------------------------------
# Rate-limit (in-memory)
# ---------------------------------------------------------------------------

_feedback_cooldown: dict[int, float] = {}
_FEEDBACK_COOLDOWN_SEC = 30

# ---------------------------------------------------------------------------
# Router (registered from main.py alongside group/private routers)
# ---------------------------------------------------------------------------

router = Router(name="feedback")


@router.message(Command("feedback"))
async def _feedback_start(message: TgMessage, state: FSMContext) -> None:
    """Step 1: User sends /feedback — ask for the text."""
    if message.from_user is None:
        return

    logger.info("/feedback command by user %s in chat %s",
                message.from_user.id, message.chat.id)

    # If user attached text in the same message — accept it directly
    inline_text = (message.text or "").partition(" ")[2].strip()
    if inline_text:
        await _process_feedback_text(message, inline_text)
        return

    await state.set_state(FeedbackStates.waiting_for_text)
    try:
        await message.reply(
            "✏️ Напишите ваш фидбек следующим сообщением (текст, вопрос или баг).\n"
            "Для отмены отправьте /cancel",
            parse_mode=None,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to send feedback prompt: %s", exc)
        await state.clear()


@router.message(Command("cancel"), StateFilter(FeedbackStates.waiting_for_text))
async def _feedback_cancel(message: TgMessage, state: FSMContext) -> None:
    """Cancel the feedback flow."""
    await state.clear()
    try:
        await message.reply("Отменено.", parse_mode=None)
    except TelegramAPIError:
        pass


@router.message(StateFilter(FeedbackStates.waiting_for_text), F.text)
async def _feedback_receive_text(message: TgMessage, state: FSMContext) -> None:
    """Step 2: User sends the feedback text."""
    await state.clear()

    if message.from_user is None:
        return

    text = (message.text or "").strip()
    if not text:
        try:
            await message.reply("Сообщение пустое. Попробуйте ещё раз: /feedback",
                                parse_mode=None)
        except TelegramAPIError:
            pass
        return

    await _process_feedback_text(message, text)


# ---------------------------------------------------------------------------
# Shared processing logic
# ---------------------------------------------------------------------------

async def _process_feedback_text(message: TgMessage, text: str) -> None:
    """Validate rate-limit, build the report, send to developer."""
    if message.from_user is None:
        return

    user_id = message.from_user.id
    bot: Bot = message.bot  # type: ignore[assignment]

    # Rate-limit
    now = time.monotonic()
    last_time = _feedback_cooldown.get(user_id, 0.0)
    if now - last_time < _FEEDBACK_COOLDOWN_SEC:
        remaining = int(_FEEDBACK_COOLDOWN_SEC - (now - last_time))
        try:
            await message.reply(
                f"⏳ Подождите ещё {remaining} сек. перед повторной отправкой фидбека.",
                parse_mode=None,
            )
        except TelegramAPIError:
            pass
        return

    _feedback_cooldown[user_id] = now

    settings = get_settings()
    if not settings.DEVELOPER_ID:
        logger.warning("DEVELOPER_ID is not configured — feedback dropped")
        try:
            await message.reply("⚠️ Функция временно недоступна.", parse_mode=None)
        except TelegramAPIError:
            pass
        return

    username = message.from_user.username or message.from_user.first_name or str(user_id)
    chat_title = message.chat.title or "Личные сообщения"
    chat_id = message.chat.id

    report = (
        "📩 <b>Новый фидбек!</b>\n"
        f"<b>От:</b> @{html.escape(username)} (ID: <code>{user_id}</code>)\n"
        f"<b>Чат:</b> {html.escape(chat_title)} (ID: <code>{chat_id}</code>)\n\n"
        f"<b>Текст:</b>\n{html.escape(text)}"
    )

    try:
        await bot.send_message(
            settings.DEVELOPER_ID,
            report,
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to deliver feedback to developer: %s", exc)
        try:
            await message.reply(
                "Не удалось доставить фидбек из-за ошибки сети. Попробуйте позже.",
                parse_mode=None,
            )
        except TelegramAPIError:
            pass
        return

    try:
        await message.reply(
            "✅ Спасибо за обратную связь! Сообщение передано разработчику.",
            parse_mode=None,
        )
    except TelegramAPIError as exc:
        logger.error("Failed to send feedback confirmation: %s", exc)
