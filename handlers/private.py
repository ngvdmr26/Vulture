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
from sqlalchemy import func, select, distinct

from database import Group, Message as DbMessage, get_session
from engine.card_generator import render_dossier
from engine.graph import get_user_metrics
from engine.profiler import generate_profile
from services.terminal_ui import safe_delete, send_card_status, send_animated_terminal

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
    "• <code>/scan</code> — сканирование эфира (мини-игра, 1 заряд)\n"
    "• <code>/bets</code> — тотализатор Судного дня\n"
    "• <code>/brand</code> — навесить клеймо на участника\n\n"
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
        from handlers.group import _dossier_delivered_users
        if message.from_user and message.from_user.id in _dossier_delivered_users:
            try:
                await message.answer(
                    "📂 <b>[VULTURE // ЛИЧНОЕ ДОСЬЕ]</b>\n\n"
                    "Ваше персональное досье уже сформировано и находится выше в этой переписке! 👆",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramAPIError:
                pass
            return
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
# Group-only command stubs (friendly error in DMs)
# ---------------------------------------------------------------------------

_DM_GROUP_ONLY = (
    "⚠️ <b>[ОШИБКА МАРШРУТА]</b>\n"
    "Данная команда работает только в групповом чате.\n"
    "Добавьте бота в группу и используйте команду там."
)

_DM_SCAN_ONLY = (
    "📡 <i>Сканирование эфира доступно только внутри защищённых периметров групповых чатов!</i>"
)


@router.message(Command("top"), F.chat.type == "private")
async def _top_private(message: TgMessage) -> None:
    """Tell user that /top only works in groups."""
    try:
        await message.answer(_DM_GROUP_ONLY, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.error("Failed to send /top DM stub: %s", exc)


@router.message(Command("sync"), F.chat.type == "private")
async def _sync_private(message: TgMessage) -> None:
    """Tell user that /sync only works in groups."""
    try:
        await message.answer(_DM_GROUP_ONLY, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.error("Failed to send /sync DM stub: %s", exc)


@router.message(Command("scan"), F.chat.type == "private")
async def _scan_private(message: TgMessage) -> None:
    """Tell user that /scan only works in groups."""
    try:
        await message.answer(_DM_SCAN_ONLY, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.error("Failed to send /scan DM stub: %s", exc)



# ---------------------------------------------------------------------------
# /stats — developer-only telemetry overview
# ---------------------------------------------------------------------------

@router.message(Command("stats"), F.chat.type == "private")
async def _stats(message: TgMessage) -> None:
    """Show global bot statistics. Restricted to DEVELOPER_ID."""
    if message.from_user is None:
        return

    from config import get_settings
    settings = get_settings()
    if not settings.DEVELOPER_ID or message.from_user.id != settings.DEVELOPER_ID:
        try:
            await message.answer(
                "⛔ <b>[ОТКАЗ В ДОСТУПЕ]</b>\n"
                "Модуль телеметрии доступен только разработчику.",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    try:
        from database import Reaction as DbReaction
        async with get_session() as session:
            # Active groups
            res_groups = await session.execute(
                select(func.count(Group.chat_id)).where(Group.is_active == True)  # noqa: E712
            )
            active_groups: int = res_groups.scalar_one() or 0

            # Total groups ever
            res_total_groups = await session.execute(select(func.count(Group.chat_id)))
            total_groups: int = res_total_groups.scalar_one() or 0

            # Unique users
            res_users = await session.execute(
                select(func.count(distinct(DbMessage.user_id)))
            )
            unique_users: int = res_users.scalar_one() or 0

            # Total messages
            res_msgs = await session.execute(select(func.count(DbMessage.message_id)))
            total_msgs: int = res_msgs.scalar_one() or 0

            # Total reactions
            res_rxns = await session.execute(select(func.count(DbReaction.id)))
            total_rxns: int = res_rxns.scalar_one() or 0

            # Top 5 chats by message count
            res_top = await session.execute(
                select(
                    DbMessage.chat_id,
                    func.count(DbMessage.message_id).label("cnt"),
                )
                .group_by(DbMessage.chat_id)
                .order_by(func.count(DbMessage.message_id).desc())
                .limit(5)
            )
            top_chats_raw = res_top.all()

            # Fetch group titles for top chats
            top_chat_ids = [r.chat_id for r in top_chats_raw]
            res_titles = await session.execute(
                select(Group.chat_id, Group.title).where(Group.chat_id.in_(top_chat_ids))
            )
            title_map = {r.chat_id: r.title for r in res_titles.all()}

    except Exception as exc:
        logger.exception("Failed to gather stats: %s", exc)
        try:
            await message.answer("⚠️ Ошибка получения статистики.", parse_mode=None)
        except TelegramAPIError:
            pass
        return

    top_lines = []
    for r in top_chats_raw:
        t = title_map.get(r.chat_id) or f"chat_{r.chat_id}"
        top_lines.append(f"  • <b>{escape(t)}</b> — {r.cnt} сообщений")

    top_block = "\n".join(top_lines) if top_lines else "  <i>Нет данных</i>"

    report = (
        "🦅 <b>VULTURE // СВОДКА ТЕЛЕМЕТРИИ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📡 <b>Активных групп:</b> {active_groups} (всего: {total_groups})\n"
        f"👥 <b>Уникальных узлов:</b> {unique_users}\n"
        f"💬 <b>Сообщений в БД:</b> {total_msgs}\n"
        f"⚡ <b>Реакций в БД:</b> {total_rxns}\n\n"
        f"🔝 <b>Топ активных секторов:</b>\n{top_block}"
    )

    try:
        await message.answer(report, parse_mode=ParseMode.HTML)
    except TelegramAPIError as exc:
        logger.error("Failed to send /stats: %s", exc)


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
    # Animated placeholder while we compute
    status_msg = await send_card_status(target, card_label="СБОРКА ПЕРСОНАЛЬНОГО ДОСЬЕ")

    try:
        await bot.send_chat_action(target.chat.id, action="upload_photo")
    except TelegramAPIError:
        pass

    # 1. Metrics
    metrics = await get_user_metrics(chat_id, user_id, days=30)
    if metrics is None:
        import random
        newcomer_quotes = [
            "Сигнал пуст. Узел еще не проявился в социальном графе.",
            "Призрак в системе. База данных пока не зафиксировала здесь следов.",
            "Тишина в эфире. Никаких следов в матрице чата.",
        ]
        chosen_quote = random.choice(newcomer_quotes)
        calib_text = (
            f"📉 <b>[КАЛИБРОВКА СЕТИ]</b>\n\n"
            f"<i>{chosen_quote}</i>\n\n"
            f"Проявите активность в чате (отвечайте на сообщения и ставьте реакции) — "
            f"и бот построит ваше персональное досье."
        )
        if status_msg:
            try:
                from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
                await status_msg.edit_text(calib_text, parse_mode=ParseMode.HTML)
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

    # 5. Delete placeholder, send photo
    await safe_delete(status_msg)

    photo = BufferedInputFile(buf.read(), filename="dossier.png")
    trend_icon = metrics.get("trend_arrow", "🔺")
    in_d = metrics.get("in_degree", 0)
    out_d = metrics.get("out_degree", 0)
    rank_idx = metrics.get("rank_in_chat", 1)

    caption = (
        f"🦅 <b>Персональное досье @{escape(str(metrics['username']))}</b> — "
        f"<i>{escape(str(profile['rank_title']))}</i>\n\n"
        f"📊 <b>Динамика:</b> {trend_icon} Ранг в группе: #{rank_idx}\n"
        f"⚖️ <b>Баланс ответов:</b> входящих [{in_d}] / исходящих [{out_d}]"
    )

    try:
        await target.answer_photo(
            photo,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        logger.debug("Failed to send dossier card for user %s", user_id)
