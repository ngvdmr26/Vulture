"""Shared /feedback handler logic.

Extracted into its own module to avoid circular imports between
the group and private routers.
"""

from __future__ import annotations

import html
import logging
import time

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.types import Message as TgMessage

from config import get_settings

logger = logging.getLogger(__name__)

# In-memory rate-limit: {user_id: last_feedback_timestamp}
_feedback_cooldown: dict[int, float] = {}
_FEEDBACK_COOLDOWN_SEC = 30


async def handle_feedback(message: TgMessage, bot: Bot) -> None:
    """Shared /feedback logic for both group and private chats."""
    if message.from_user is None:
        return

    user_id = message.from_user.id
    text = (message.text or "").partition(" ")[2].strip()

    if not text:
        try:
            await message.reply(
                "Использование: /feedback <ваш текст, вопрос или найденный баг>",
            )
        except TelegramAPIError:
            pass
        return

    # Rate-limit check
    now = time.monotonic()
    last_time = _feedback_cooldown.get(user_id, 0.0)
    if now - last_time < _FEEDBACK_COOLDOWN_SEC:
        remaining = int(_FEEDBACK_COOLDOWN_SEC - (now - last_time))
        try:
            await message.reply(
                f"⏳ Подождите ещё {remaining} сек. перед повторной отправкой фидбека.",
            )
        except TelegramAPIError:
            pass
        return

    _feedback_cooldown[user_id] = now

    settings = get_settings()
    if not settings.DEVELOPER_ID:
        logger.warning("DEVELOPER_ID is not configured — feedback dropped")
        try:
            await message.reply("⚠️ Функция временно недоступна.")
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
            )
        except TelegramAPIError:
            pass
        return

    try:
        await message.reply("Спасибо за обратную связь! Сообщение передано разработчику.")
    except TelegramAPIError:
        pass
