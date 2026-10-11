"""Vulture Terminal UI — pseudo-animated message rendering.

Provides a unified pipeline for all bot text responses, creating
a cyberpunk terminal typewriter effect via sequential edit_text calls.
All edit/delete calls are wrapped against TelegramBadRequest and
TelegramRetryAfter to prevent crashes under Telegram rate limits.
"""

from __future__ import annotations

import asyncio
import logging

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter, TelegramAPIError
from aiogram.types import Message as TgMessage

logger = logging.getLogger(__name__)

# How long each animation frame shows (seconds)
_FRAME_DELAY = 0.35


async def send_animated_terminal(
    message: TgMessage,
    final_text: str,
    operation_name: str = "ИНИЦИАЛИЗАЦИЯ",
    parse_mode: str = ParseMode.HTML,
) -> TgMessage | None:
    """Send a message with a 3-frame cyberpunk terminal animation.

    Steps:
      1. reply() with a booting frame
      2. edit_text() with a decryption frame
      3. edit_text() with the final content

    Returns the final Message object, or None if the initial reply failed.
    All intermediate edit_text calls are skipped gracefully on FloodWait.
    """
    # --- Frame 1: boot ---
    frame1 = (
        f"<code>&gt; [VULTURE://KERNEL] Выполняется: {operation_name}... █</code>"
    )
    try:
        sent = await message.reply(frame1, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.debug("Terminal UI: failed to send boot frame: %s", exc)
        return None

    # --- Frame 2: decryption ---
    frame2 = "<code>&gt; [VULTURE://KERNEL] Дешифровка потока данных... ▓▒░</code>"
    try:
        await asyncio.sleep(_FRAME_DELAY)
        await sent.edit_text(frame2, parse_mode=ParseMode.HTML)
    except TelegramRetryAfter:
        # Rate-limited — skip intermediate frames, jump to final
        logger.debug("Terminal UI: FloodWait on frame 2, skipping to final")
        _safe_edit_sync = True
    except (TelegramBadRequest, TelegramAPIError):
        pass
    else:
        _safe_edit_sync = False

    # --- Frame 3: final content ---
    try:
        await asyncio.sleep(_FRAME_DELAY)
        await sent.edit_text(final_text, parse_mode=parse_mode)
    except TelegramRetryAfter as exc:
        logger.debug("Terminal UI: FloodWait on final frame (%s s), sending as new message", exc.retry_after)
        # As last resort, send as a new separate message
        try:
            await sent.delete()
        except TelegramAPIError:
            pass
        try:
            return await message.reply(final_text, parse_mode=parse_mode)
        except TelegramAPIError:
            return None
    except (TelegramBadRequest, TelegramAPIError) as exc:
        logger.debug("Terminal UI: failed to edit final frame: %s", exc)

    return sent


async def send_progress_bar(
    message: TgMessage,
    current: int,
    total: int,
    label: str = "ИНИЦИАЛИЗАЦИЯ НЕЙРОСЕТЕВОГО ГРАФА",
) -> TgMessage | None:
    """Send a terminal-style progress bar with a 2-frame animation.

    Frame 1: empty bar at 0%
    Frame 2 (after 0.4s): real progress bar at current/total %
    """
    pct = min(100, int((current / total) * 100)) if total > 0 else 0
    filled = int(pct / 10)
    empty = 10 - filled
    bar_final = "■" * filled + "□" * empty

    # Frame 1: empty bar
    empty_bar = "□" * 10
    frame1 = (
        f"⏳ <b>[VULTURE://KERNEL] {label}</b>\n"
        f"<code>Синхронизация узлов: [{empty_bar}] 0%</code>"
    )
    try:
        sent = await message.reply(frame1, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.debug("Progress bar: failed to send empty bar: %s", exc)
        return None

    # Frame 2: real progress
    frame2 = (
        f"⏳ <b>[VULTURE://KERNEL] {label}</b>\n"
        f"<code>Калибровка узлов: [{bar_final}] {pct}% ({current}/{total} связей)</code>\n\n"
        f"<i>Протоколу требуется еще немного активности (ответов и реакций "
        f"друг на друга) для построения устойчивой топологии.</i>"
    )
    try:
        await asyncio.sleep(0.4)
        await sent.edit_text(frame2, parse_mode=ParseMode.HTML)
    except TelegramRetryAfter:
        logger.debug("Progress bar: FloodWait, keeping empty bar as final")
    except (TelegramBadRequest, TelegramAPIError) as exc:
        logger.debug("Progress bar: failed to edit final frame: %s", exc)

    return sent


async def send_card_status(
    message: TgMessage,
    card_label: str = "ГЕНЕРАЦИЯ ВИЗУАЛЬНОЙ КАРТОЧКИ",
) -> TgMessage | None:
    """Send an animated 'generating card' placeholder for image commands.

    Returns the placeholder Message so the caller can delete it after
    the Pillow render finishes.
    """
    frame1 = (
        f"<code>&gt; [VULTURE://RENDER] {card_label}... █</code>"
    )
    try:
        sent = await message.reply(frame1, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.debug("Card status: failed to send frame 1: %s", exc)
        return None

    frame2 = "<code>&gt; [VULTURE://RENDER] Компиляция данных узлов... ▓▒░</code>"
    try:
        await asyncio.sleep(_FRAME_DELAY)
        await sent.edit_text(frame2, parse_mode=ParseMode.HTML)
    except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError):
        pass

    return sent


async def safe_delete(msg: TgMessage | None) -> None:
    """Delete a message silently, ignoring all errors."""
    if msg is None:
        return
    try:
        await msg.delete()
    except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError):
        pass
