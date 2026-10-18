"""Vulture Event Interceptions — automatic anomaly announcements in group chat.

Fires at most one notification per 2.5 hours per group.
Requires at least 12 messages in the last 15 minutes (live chat check).

Triggers:
  1. Leader change in top-3 influence ranking
  2. Anomalous resonance: >= 10 replies between two people in 15 min (+ /sync button)
  3. Protocol of silence: >= 5 messages from user, 0 replies received
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.utils.keyboard import InlineKeyboardBuilder

logger = logging.getLogger(__name__)

# In-memory tracking per chat_id
_last_interception: dict[int, datetime] = {}
_last_top3: dict[int, list[int]] = {}

_COOLDOWN_SECONDS = 2.5 * 60 * 60  # 2.5 hours


async def maybe_fire_interception(
    bot: Bot,
    chat_id: int,
    user_map: dict[int, str] | None = None,
) -> None:
    """Check interception conditions and fire at most one notification.

    Safe fire-and-forget entrypoint. Never raises exceptions to the caller.
    """
    try:
        await _check_and_fire(bot, chat_id, user_map)
    except Exception:
        logger.exception("Interception check failed for chat %s", chat_id)


async def _check_and_fire(
    bot: Bot,
    chat_id: int,
    user_map: dict[int, str] | None,
) -> None:
    from engine.graph import get_interception_data

    now = datetime.now(timezone.utc)
    last = _last_interception.get(chat_id)
    if last and (now - last).total_seconds() < _COOLDOWN_SECONDS:
        return

    data = await get_interception_data(chat_id, window_minutes=15)

    if data["message_count_15m"] < 12:
        return

    umap = user_map or {}
    fired = False

    def _fmt_user(uid: int) -> str:
        """Return @username if known, otherwise a clickable tg:// profile link."""
        name = umap.get(uid)
        if name and not name.startswith("id_"):
            return f"@{name}"
        return f'<a href="tg://user?id={uid}">Участник</a>'

    # --- Trigger 1: Leader change in top-3 ---
    top3 = data["top3_uids"]
    old_top3 = _last_top3.get(chat_id, [])
    if (
        top3
        and old_top3
        and len(top3) >= 2
        and len(old_top3) >= 2
        and (top3[0] != old_top3[0] or top3[1] != old_top3[1])
    ):
        leader = _fmt_user(top3[0])
        second = _fmt_user(top3[1])
        try:
            await bot.send_message(
                chat_id,
                f"⚡ <b>[VULTURE // ПЕРЕХВАТ ВЛАСТИ]</b>\n\n"
                f"В иерархии чата произошел сдвиг. Новый лидер влияния:\n"
                f"👑 <b>{leader}</b> — захватил вершину графа\n"
                f"#2: <b>{second}</b>\n\n"
                f"<i>Данные обновлены в реальном времени по паттернам последних 7 дней.</i>",
                parse_mode=ParseMode.HTML,
            )
            fired = True
        except TelegramAPIError:
            logger.debug("Failed to send leader-change interception to chat %s", chat_id)
    _last_top3[chat_id] = top3

    if fired:
        _last_interception[chat_id] = now
        return

    # --- Trigger 2: Anomalous resonance (>= 10 replies in 15 min) ---
    for uid_a, uid_b, cnt in data["hot_pairs"]:
        name_a = _fmt_user(uid_a)
        name_b = _fmt_user(uid_b)
        uname_a = umap.get(uid_a, "")
        uname_b = umap.get(uid_b, "")
        btn_label_a = f"@{uname_a}" if uname_a and not uname_a.startswith("id_") else f"Участник {uid_a}"
        btn_label_b = f"@{uname_b}" if uname_b and not uname_b.startswith("id_") else f"Участник {uid_b}"
        builder = InlineKeyboardBuilder()
        builder.button(
            text=f"🔗 Проверить связь {btn_label_a} + {btn_label_b}",
            callback_data=f"sync:{uid_a}:{uid_b}",
        )
        try:
            await bot.send_message(
                chat_id,
                f"🚨 <b>[VULTURE // АНОМАЛЬНЫЙ РЕЗОНАНС]</b>\n\n"
                f"Зафиксировано <b>{cnt}</b> ответов на сообщения между двумя узлами за 15 минут.\n"
                f"Подозреваемые: <b>{name_a}</b> ↔ <b>{name_b}</b>\n\n"
                f"<i>Уровень синхронизации превышает норму в 3–4 раза.</i>",
                parse_mode=ParseMode.HTML,
                reply_markup=builder.as_markup(),
            )
            fired = True
        except TelegramAPIError:
            logger.debug("Failed to send hot-pair interception to chat %s", chat_id)
        break  # At most one announcement

    if fired:
        _last_interception[chat_id] = now
        return

    # --- Trigger 3: Protocol of silence (5+ messages, 0 replies) ---
    for uid in data["silent_victims"]:
        name = _fmt_user(uid)
        try:
            await bot.send_message(
                chat_id,
                f"💀 <b>[VULTURE // ПРОТОКОЛ ТИШИНЫ]</b>\n\n"
                f"Узел <b>{name}</b> активно пишет, но не получает ответов.\n"
                f"5+ сообщений подряд — ноль реакций. Социальная изоляция зафиксирована.\n\n"
                f"<i>Это нормальное явление при доминировании одного-двух голосов в чате.</i>",
                parse_mode=ParseMode.HTML,
            )
            fired = True
        except TelegramAPIError:
            logger.debug("Failed to send silence-protocol interception to chat %s", chat_id)
        break  # At most one announcement

    if fired:
        _last_interception[chat_id] = now
