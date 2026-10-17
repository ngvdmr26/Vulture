"""Aiogram 3.x router for group-chat events.

Commands: /sync, /top, /dossier, /brand, /bets.
Telemetry: silent message & reaction logging.
Mini-game: Детектор притяжения (/dossier guessing game) + Глушилка.
Totalizator: Судный день betting and resolution.
Auto-calibration: fires a one-time notification when the graph crosses
the MIN_INTERACTIONS threshold for the first time.
"""

from __future__ import annotations

import asyncio
import html
import logging
import random
from datetime import datetime, timedelta, timezone

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

from database import (
    DossierGame,
    DossierGameVote,
    Group,
    Message,
    PurgeBet,
    Reaction,
    UserGroupStats,
    get_or_create_user_stats,
    get_session,
)
from engine.card_generator import render_dossier, render_sync_card
from engine.graph import (
    _MIN_INTERACTIONS,
    get_graph_edge_count,
    get_group_leaderboard,
    get_leaderboard_with_archetypes,
    get_pair_metrics,
    get_top_active_users,
    get_user_metrics,
)
from engine.interceptions import maybe_fire_interception
from engine.profiler import generate_profile
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
    "• <code>/dossier</code> — личное психологическое досье\n"
    "• <code>/bets</code> — тотализатор Судного дня\n"
    "• <code>/brand</code> — навесить клеймо на участника\n\n"
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

NEWCOMER_QUOTES = [
    "Сигнал пуст. Узел еще не проявился в социальном графе.",
    "Призрак в системе. База данных пока не зафиксировала здесь следов.",
    "Тишина в эфире. Никаких следов в матрице чата.",
]

SELF_VOTE_QUOTES = [
    "Авто-привязка отклонена. Искать самого себя в чужих досье запрещено алгоритмом.",
    "Эго-фильтр активирован. Выберите другую цель в матрице.",
]

JAMMER_ROAST_QUOTES = [
    "Ага, классика. Кому-то явно есть что скрывать от социального графа...",
    "Глушилка активирована. Кто-то слишком сильно боится правды 🕵️‍♂️",
    "Секретный уровень защиты включен. Паранойя вышла на новый уровень!",
    "Защитная завеса поставлена. Но мы-то знаем... 😉",
    "Сигнал заблокирован! Пущены в ход запрещенные приемы.",
]


def _resolve_username(user) -> str:
    """Return the best available display name, never None."""
    if user is None:
        return "unknown"
    return user.username or user.first_name or f"id_{user.id}"


def is_betting_open(now: datetime | None = None) -> bool:
    """Check if betting is currently open (Saturday 20:00 MSK to Sunday 20:00 MSK)."""
    if now is None:
        now = datetime.now(timezone.utc)
    # Saturday 17:00 UTC (20:00 MSK) onwards
    if now.weekday() == 5 and now.hour >= 17:
        return True
    # Sunday before 17:00 UTC (20:00 MSK)
    if now.weekday() == 6 and now.hour < 17:
        return True
    return False


# ---------------------------------------------------------------------------
# Progress bar helper
# ---------------------------------------------------------------------------

async def _show_calibration_progress(message: TgMessage, chat_id: int) -> bool:
    """Show progress bar if under threshold. Returns True if shown."""
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
    """Send personal dossier to user's DM and launch detector mini-game in group."""
    if message.from_user is None:
        return

    chat_id = message.chat.id
    user_id = message.from_user.id
    username = _resolve_username(message.from_user)

    metrics = await get_user_metrics(chat_id, user_id, days=30)
    if not metrics:
        quote = random.choice(NEWCOMER_QUOTES)
        text = (
            f"📉 <b>[КАЛИБРОВКА СЕТИ]</b>\n\n"
            f"<i>{quote}</i>\n\n"
            f"Проявите активность в чате (отвечайте на сообщения и ставьте реакции) — "
            f"и бот построит ваше персональное досье."
        )
        try:
            await message.reply(text, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass
        return

    # Attempt to deliver private dossier to DM
    try:
        profile = await generate_profile(metrics)
        card_data = {**metrics, **profile}
        me = await bot.get_me()
        buf = await render_dossier(card_data, bot_username=me.username or "VultureBot")
        buf.seek(0)
        photo = BufferedInputFile(buf.read(), filename="dossier.png")
        trend_icon = metrics.get("trend_arrow", "🔺")
        in_d = metrics.get("in_degree", 0)
        out_d = metrics.get("out_degree", 0)
        rank_idx = metrics.get("rank_in_chat", 1)

        dm_caption = (
            f"🦅 <b>Персональное досье @{html.escape(str(metrics['username']))}</b> — "
            f"<i>{html.escape(str(profile['rank_title']))}</i>\n\n"
            f"📊 <b>Динамика:</b> {trend_icon} Ранг в группе: #{rank_idx}\n"
            f"⚖️ <b>Баланс ответов:</b> входящих [{in_d}] / исходящих [{out_d}]"
        )
        await bot.send_photo(
            chat_id=user_id,
            photo=photo,
            caption=dm_caption,
            parse_mode=ParseMode.HTML,
        )
    except Exception:
        logger.debug("Could not deliver DM dossier to user %s", user_id)

    # Launch detector mini-game directly in group (only one neat interactive message)
    await _start_dossier_game(message, bot, chat_id, user_id, username, metrics)


async def _start_dossier_game(
    message: TgMessage,
    bot: Bot,
    chat_id: int,
    user_id: int,
    username: str,
    metrics: dict,
) -> None:
    """Launch a new active 'Детектор притяжения' mini-game session."""
    try:
        top_target = metrics.get("top_targeted_user")
        if not top_target or top_target.startswith("id_"):
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)

        async with get_session() as session:
            stats = await get_or_create_user_stats(session, chat_id, user_id, username)
            stats.last_dossier_game_at = now

            res_users_cnt = await session.execute(
                select(Message.username)
                .where(Message.chat_id == chat_id, Message.username != "")
                .distinct()
            )
            distinct_usernames = [
                u for u in res_users_cnt.scalars().all()
                if u and u.lower() != username.lower() and u.lower() != top_target.lower()
            ]
            total_chat_users = len(distinct_usernames) + 2
            threshold = 3 if total_chat_users < 6 else 5

            if len(distinct_usernames) < 3:
                return

            decoys = random.sample(distinct_usernames, 3)
            options = [top_target] + decoys
            random.shuffle(options)

            game = DossierGame(
                chat_id=chat_id,
                subject_user_id=user_id,
                subject_username=username,
                correct_answer=top_target,
                message_id=0,
                is_active=True,
                vote_count=0,
                threshold=threshold,
                created_at=now,
            )
            session.add(game)
            await session.commit()
            await session.refresh(game)
            game_id = game.id

        me = await bot.get_me()
        builder = InlineKeyboardBuilder()
        for opt in options:
            builder.button(text=f"@{opt}", callback_data=f"dg_vote:{game_id}:{opt}")
        builder.button(text="🛡 Включить глушилку", callback_data=f"dg_jam:{game_id}")
        builder.button(text="📂 Открыть своё досье в ЛС", url=f"https://t.me/{me.username}?start=dossier")
        builder.adjust(2, 2, 1, 1)

        text = (
            f"🕵️‍♂️ <b>ПРОТОКОЛ ПЕРЕХВАТА // @{html.escape(username)}</b>\n"
            f"Объект запросил секретное досье в ЛС. Зафиксирована сильная гравитационная привязка.\n\n"
            f"🎯 <b>Угадайте, кто сейчас Главная цель гравитации @{html.escape(username)}?</b>\n"
            f"<i>Досье раскроется автоматически, как только наберется {threshold} голосов!</i>"
        )

        sent_msg = await bot.send_message(
            chat_id,
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=builder.as_markup(),
        )

        async with get_session() as session:
            res_g = await session.execute(select(DossierGame).where(DossierGame.id == game_id))
            g_row = res_g.scalar_one_or_none()
            if g_row:
                g_row.message_id = sent_msg.message_id
                await session.commit()

    except Exception:
        logger.exception("Failed to launch dossier game for user %s in chat %s", user_id, chat_id)


@router.callback_query(F.data.startswith("dg_vote:"))
async def _dg_vote_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Handle votes in the dossier guessing mini-game."""
    if callback.from_user is None or callback.message is None:
        return

    try:
        _, game_id_str, voted_for = callback.data.split(":", 2)
        game_id = int(game_id_str)
    except Exception:
        try:
            await callback.answer("⚠️ Неверный формат данных.", show_alert=True)
        except TelegramAPIError:
            pass
        return

    voter_id = callback.from_user.id
    voter_username = _resolve_username(callback.from_user)

    async with get_session() as session:
        res = await session.execute(select(DossierGame).where(DossierGame.id == game_id))
        game = res.scalar_one_or_none()
        if not game or not game.is_active:
            try:
                await callback.answer("Голосование уже завершено.", show_alert=True)
            except TelegramAPIError:
                pass
            return

        if voter_id == game.subject_user_id:
            try:
                await callback.answer(random.choice(SELF_VOTE_QUOTES), show_alert=True)
            except TelegramAPIError:
                pass
            return

        res_vote = await session.execute(
            select(DossierGameVote).where(
                DossierGameVote.game_id == game_id,
                DossierGameVote.voter_id == voter_id,
            )
        )
        if res_vote.scalar_one_or_none():
            try:
                await callback.answer("Вы уже отдали свой голос!", show_alert=True)
            except TelegramAPIError:
                pass
            return

        is_correct = (voted_for.lower().strip("@") == game.correct_answer.lower().strip("@"))
        vote = DossierGameVote(
            game_id=game_id,
            voter_id=voter_id,
            voted_for=voted_for,
            is_correct=is_correct,
        )
        session.add(vote)
        game.vote_count += 1
        await session.commit()

        cur_votes = game.vote_count
        threshold = game.threshold
        is_finished = cur_votes >= threshold

        if is_finished:
            game.is_active = False
            await session.commit()

            res_correct = await session.execute(
                select(DossierGameVote).where(
                    DossierGameVote.game_id == game_id,
                    DossierGameVote.is_correct.is_(True),
                )
            )
            correct_votes = list(res_correct.scalars().all())
            for cv in correct_votes:
                stats = await get_or_create_user_stats(session, game.chat_id, cv.voter_id)
                stats.bonus_influence = (stats.bonus_influence or 0.0) + 0.5
                stats.correct_guesses_count = (stats.correct_guesses_count or 0) + 1
                if stats.correct_guesses_count % 3 == 0:
                    stats.jammer_charges = (stats.jammer_charges or 0) + 1
            await session.commit()

            winner_count = len(correct_votes)
            subject_name = game.subject_username
            correct_ans = game.correct_answer

            reveal_text = (
                f"🎯 <b>ПРОТОКОЛ ПЕРЕХВАТА // РЕЗУЛЬТАТ</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Порог в {threshold} голосов набран!\n"
                f"Главная цель гравитации @{html.escape(subject_name)} — <b>@{html.escape(correct_ans)}</b>!\n\n"
                f"Правильно угадали: <b>{winner_count}</b> из {threshold} участников.\n"
                f"Победители получили +0.5 к влиянию и прогресс Глушилки (+1 заряд за каждые 3 отгадки)."
            )
            try:
                await callback.message.edit_text(reveal_text, parse_mode=ParseMode.HTML)
            except TelegramAPIError:
                pass

        try:
            await callback.answer(f"Голос принят! ({cur_votes}/{threshold})")
        except TelegramAPIError:
            pass


@router.callback_query(F.data.startswith("dg_jam:"))
async def _dg_jam_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Handle jammer activation in dossier mini-game."""
    if callback.from_user is None or callback.message is None:
        return

    try:
        _, game_id_str = callback.data.split(":", 1)
        game_id = int(game_id_str)
    except Exception:
        return

    user_id = callback.from_user.id

    async with get_session() as session:
        res = await session.execute(select(DossierGame).where(DossierGame.id == game_id))
        game = res.scalar_one_or_none()
        if not game or not game.is_active:
            try:
                await callback.answer("Игра уже завершена.", show_alert=True)
            except TelegramAPIError:
                pass
            return

        if user_id != game.subject_user_id:
            try:
                await callback.answer("Только владелец досье может активировать глушилку!", show_alert=True)
            except TelegramAPIError:
                pass
            return

        stats = await get_or_create_user_stats(session, game.chat_id, user_id, callback.from_user.username)
        if (stats.jammer_charges or 0) <= 0:
            try:
                await callback.answer(
                    "У вас нет зарядов Глушилки! Они пополняются раз в 24ч или за правильные угадывания чужих целей.",
                    show_alert=True,
                )
            except TelegramAPIError:
                pass
            return

        stats.jammer_charges -= 1
        game.is_active = False
        remaining = stats.jammer_charges
        await session.commit()
        subj = game.subject_username

    random_quote = random.choice(JAMMER_ROAST_QUOTES)
    jam_text = (
        f"🛡 <b>[ГЛУШИЛКА АКТИВИРОВАНА]</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Объект @{html.escape(subj)} задействовал систему радиоэлектронного подавления!\n"
        f"Данные о главной связи зашифрованы. Попытка перехвата нейтрализована.\n\n"
        f"Списан 1 заряд Глушилки (осталось: {remaining}).\n\n"
        f"<i>{random_quote}</i>"
    )

    try:
        await callback.message.edit_text(jam_text, parse_mode=ParseMode.HTML, reply_markup=None)
    except TelegramAPIError:
        pass

    try:
        await callback.answer("Глушилка активирована! Данные скрыты.")
    except TelegramAPIError:
        pass


@router.message(Command("top"), _GROUP_FILTER)
async def _top(message: TgMessage, bot: Bot) -> None:
    """Show group influence leaderboard with archetypes and trends."""
    chat_id = message.chat.id

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

    leaderboard = await get_leaderboard_with_archetypes(chat_id, limit=7)
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
            f"{icon} <b>@{html.escape(user['username'])}</b> — {user['final_score']} баллов {user['trend_arrow']}\n"
            f"   <i>{user['archetype_label']}</i>"
        )

    builder = InlineKeyboardBuilder()
    builder.button(text="📖 Как считается этот рейтинг?", callback_data="top_help")

    final_text = "\n".join(lines)
    if status_msg:
        try:
            await status_msg.edit_text(final_text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
        except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError):
            logger.debug("Unable to update top status in chat %s", chat_id)
    else:
        try:
            await message.reply(final_text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
        except TelegramAPIError:
            pass


@router.callback_query(F.data == "top_help")
async def _top_help_callback(callback: CallbackQuery) -> None:
    """Show detailed explanation of the ranking calculation."""
    help_text = (
        "📖 <b>КАК РАССЧИТЫВАЕТСЯ РЕЙТИНГ VULTURE</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "<b>Баллы влияния (0–10):</b> Рассчитываются по алгоритму PageRank за последние 30 дней. "
        "Учитываются только живые ответы на ваши сообщения и реакции. Простой спам без откликов баллов не приносит!\n\n"
        "<b>Стрелки тренда (🔺 / 🔻):</b> Показывают динамику: 🔺 — активность за последние 24 часа выше вашей недельной нормы; "
        "🔻 — активность снижается.\n\n"
        "<b>Роли и статусы:</b> Уникальные архетипы (Серый кардинал, Магнит внимания, Снайпер и др.) распределяются алгоритмом без повторов.\n\n"
        "<b>Чёрная метка (/brand):</b> Победители субботнего Тотализатора получают эксклюзивное право лично переписать статус любого участника на 72 часа!"
    )
    try:
        await callback.answer()
        await callback.message.reply(help_text, parse_mode=ParseMode.HTML)
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------------------
# /sync — with inline keyboard picker, roulette and robust photo sending
# ---------------------------------------------------------------------------

@router.message(Command("sync"), _GROUP_FILTER)
async def _sync(message: TgMessage, bot: Bot) -> None:
    """Pair analysis between invoker and replied user or tagged username."""
    if message.from_user is None:
        return

    chat_id = message.chat.id
    user_a_id = message.from_user.id
    user_b_id: int | None = None

    if message.reply_to_message and message.reply_to_message.from_user:
        user_b_id = message.reply_to_message.from_user.id

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

    if user_b_id is None or user_b_id == user_a_id:
        await _sync_show_picker(message, chat_id, user_a_id)
        return

    await _sync_run(bot, chat_id, user_a_id, user_b_id, trigger_message=message)


async def _sync_show_picker(message: TgMessage, chat_id: int, caller_id: int) -> None:
    """Show inline keyboard with top-6 active users and roulette for /sync."""
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
                "Недостаточно участников для выбора. Ответьте на сообщение участника командой "
                "<code>/sync</code> или напишите <code>/sync @username</code>.",
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
    builder.adjust(2)
    builder.row()
    builder.button(
        text="🎲 Случайный резонанс (Рулетка)",
        callback_data=f"sync_random:{caller_id}",
    )

    try:
        await message.reply(
            "🔭 <b>[VULTURE // СКАНЕР СВЯЗЕЙ]</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Выберите участника для анализа парной связи:\n\n"
            "<i>Или: ответьте на сообщение участника командой /sync · /sync @username</i>",
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
        _, caller_id_str, target_id_str = callback.data.split(":")
        caller_id = int(caller_id_str)
        target_id = int(target_id_str)
    except (ValueError, AttributeError):
        try:
            await callback.answer("⚠️ Неверный формат данных.", show_alert=True)
        except TelegramAPIError:
            pass
        return

    if callback.from_user.id != caller_id:
        try:
            await callback.answer("Это сканирование запущено другим пользователем.", show_alert=True)
        except TelegramAPIError:
            pass
        return

    try:
        await callback.answer()
    except TelegramAPIError:
        pass

    try:
        await callback.message.delete()
    except (TelegramBadRequest, TelegramRetryAfter, TelegramAPIError):
        pass

    await _sync_run(bot, callback.message.chat.id, caller_id, target_id)


@router.callback_query(F.data.startswith("sync_random:"))
async def _sync_random_callback(callback: CallbackQuery, bot: Bot) -> None:
    """Handle random target roulette for /sync."""
    if callback.from_user is None or callback.message is None:
        return

    try:
        _, caller_id_str = callback.data.split(":", 1)
        caller_id = int(caller_id_str)
    except Exception:
        return

    if callback.from_user.id != caller_id:
        try:
            await callback.answer("Это сканирование запущено другим пользователем.", show_alert=True)
        except TelegramAPIError:
            pass
        return

    chat_id = callback.message.chat.id
    try:
        async with get_session() as session:
            res = await session.execute(
                select(Message.user_id)
                .where(Message.chat_id == chat_id, Message.user_id != caller_id)
                .distinct()
            )
            uids = list(res.scalars().all())
    except Exception:
        uids = []

    if not uids:
        try:
            await callback.answer("Недостаточно участников в базе для рулетки.", show_alert=True)
        except TelegramAPIError:
            pass
        return

    chosen_target = random.choice(uids)

    try:
        await callback.answer("🎲 Рулетка выбрала участника!")
        await callback.message.delete()
    except TelegramAPIError:
        pass

    await _sync_run(bot, chat_id, caller_id, chosen_target)


async def _sync_run(
    bot: Bot,
    chat_id: int,
    user_a_id: int,
    user_b_id: int,
    trigger_message: TgMessage | None = None,
) -> None:
    """Build and send the sync card via BufferedInputFile directly to chat."""
    status_msg = None
    if trigger_message:
        status_msg = await send_card_status(trigger_message, card_label="ГЕНЕРАЦИЯ КАРТОЧКИ СВЯЗЕЙ")

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
        if status_msg:
            await safe_delete(status_msg)
        try:
            await bot.send_message(chat_id, calib_text, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass
        return

    me = await bot.get_me()
    buf = await render_sync_card(pair_data, bot_username=me.username or "VultureBot")
    buf.seek(0)
    photo_file = BufferedInputFile(buf.read(), filename="sync.png")

    if status_msg:
        await safe_delete(status_msg)

    caption = (
        f"🔗 <b>VULTURE // СИНХРОНИЗАЦИЯ УЗЛОВ</b>\n"
        f"@{html.escape(pair_data['user_a'])} ↔ @{html.escape(pair_data['user_b'])}\n"
        f"Резонанс: <b>{pair_data.get('sync_percent', 0.0):.1f}%</b> — {html.escape(pair_data.get('verdict', ''))}"
    )

    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo_file,
            caption=caption,
            parse_mode=ParseMode.HTML,
        )
    except TelegramAPIError:
        logger.exception("Failed to send sync photo in chat %s", chat_id)


# ---------------------------------------------------------------------------
# /brand — Custom Status ("Чёрная метка")
# ---------------------------------------------------------------------------

@router.message(Command("brand"), _GROUP_FILTER)
async def _brand(message: TgMessage, bot: Bot) -> None:
    """Assign custom status to another user if caller has brand charges."""
    if message.from_user is None:
        return

    chat_id = message.chat.id
    caller_id = message.from_user.id
    caller_name = _resolve_username(message.from_user)

    text_parts = (message.text or "").split(maxsplit=2)
    target_username: str | None = None
    status_text: str | None = None

    if message.reply_to_message and message.reply_to_message.from_user:
        target_username = _resolve_username(message.reply_to_message.from_user)
        if len(text_parts) > 1:
            status_text = (message.text or "").split(maxsplit=1)[1].strip()
    elif len(text_parts) >= 3 and text_parts[1].startswith("@"):
        target_username = text_parts[1].lstrip("@")
        status_text = text_parts[2].strip()

    if not target_username or not status_text:
        try:
            await message.reply(
                "🏷 <b>[VULTURE // ЧЁРНАЯ МЕТКА]</b>\n\n"
                "Использование:\n"
                "• <code>/brand @username Текст статуса</code>\n"
                "• или ответом на сообщение: <code>/brand Текст статуса</code>\n\n"
                "<i>Заряды Чёрной метки выдаются только победителям воскресного Тотализатора (/bets).</i>",
                parse_mode=ParseMode.HTML,
            )
        except TelegramAPIError:
            pass
        return

    if len(status_text) > 40:
        status_text = status_text[:40]

    async with get_session() as session:
        caller_stats = await get_or_create_user_stats(session, chat_id, caller_id, caller_name)
        if (caller_stats.brand_charges or 0) <= 0:
            try:
                await message.reply(
                    "⚠️ <b>[НЕДОСТАТОЧНО ПРАВ]</b>\n\n"
                    "У вас <b>0 зарядов</b> Чёрной метки.\n"
                    "Заряды получают только победители воскресного Тотализатора Судного дня!\n"
                    "Делайте ставки по субботам через <code>/bets</code>.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramAPIError:
                pass
            return

        res_t = await session.execute(
            select(Message.user_id)
            .where(Message.chat_id == chat_id, Message.username == target_username)
            .limit(1)
        )
        target_uid = res_t.scalar_one_or_none()
        if not target_uid:
            try:
                await message.reply(
                    f"⚠️ Участник @{html.escape(target_username)} не найден в базе активности этой группы.",
                    parse_mode=ParseMode.HTML,
                )
            except TelegramAPIError:
                pass
            return

        target_stats = await get_or_create_user_stats(session, chat_id, target_uid, target_username)

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        caller_stats.brand_charges -= 1
        target_stats.custom_status = status_text
        target_stats.custom_status_by = caller_name
        target_stats.custom_status_until = now + timedelta(hours=72)
        await session.commit()

    brand_announcement = (
        f"🏷 <b>[VULTURE // ЧЁРНАЯ МЕТКА]</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"Узел @{html.escape(caller_name)} применил право Чёрной метки к @{html.escape(target_username)}!\n\n"
        f"Присвоен статус: <b>«{html.escape(status_text)}»</b>\n"
        f"⏳ Срок действия: <b>72 часа</b>.\n\n"
        f"<i>Этот статус зафиксирован протоколом и будет отображаться во всех сводках /top.</i>"
    )
    try:
        await message.reply(brand_announcement, parse_mode=ParseMode.HTML)
    except TelegramAPIError:
        pass


# ---------------------------------------------------------------------------
# /bets & Persistent Interactive Totalizator
# ---------------------------------------------------------------------------

CAT_TITLES = {
    "top_influencer": "👑 Серый кардинал",
    "clown_of_the_week": "🤡 Клоун недели",
    "neglected": "💀 Главный игнор",
}


def _build_nominations_keyboard() -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.button(text="👑 Серый кардинал", callback_data="bet_cat:top_influencer")
    builder.button(text="🤡 Клоун недели", callback_data="bet_cat:clown_of_the_week")
    builder.button(text="💀 Главный игнор", callback_data="bet_cat:neglected")
    builder.adjust(1)
    return builder


@router.message(Command("bets"), _GROUP_FILTER)
async def _bets(message: TgMessage, bot: Bot) -> None:
    """Show Totalizator betting prompt or closed notification."""
    chat_id = message.chat.id

    if not is_betting_open():
        closed_text = (
            "🎰 <b>[ТОТАЛИЗАТОР СУДНОГО ДНЯ // ЗАКРЫТ]</b>\n"
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            "Окно приёма ставок открывается каждую <b>субботу в 20:00 МСК</b> и закрывается в <b>воскресенье в 20:00 МСК</b>.\n\n"
            "🏆 <b>Призы для угадавших:</b>\n"
            "• +1 заряд <b>Глушилки</b> (защита от раскрытия связей)\n"
            "• +1 заряд <b>Чёрной метки</b> (команда /brand)\n"
            "• +1.5 балла к рейтингу влияния\n\n"
            "<i>Возвращайтесь в субботу вечером, чтобы сделать свой прогноз!</i>"
        )
        try:
            await message.reply(closed_text, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            pass
        return

    text = (
        "🎰 <b>VULTURE // ТОТАЛИЗАТОР СУДНОГО ДНЯ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Угадайте победителей воскресного разбора Судного дня!\n\n"
        "🏆 <b>Призы для победителей:</b>\n"
        "• +1 заряд <b>Глушилки</b> (защита от раскрытия связей)\n"
        "• +1 заряд <b>Чёрной метки</b> (команда /brand)\n"
        "• +1.5 балла к рейтингу влияния\n\n"
        "Выберите номинацию для ставки (при повторном выборе ставка перезаписывается):"
    )
    builder = _build_nominations_keyboard()
    try:
        await message.reply(text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
    except TelegramAPIError:
        pass


@router.callback_query(F.data == "bet_back")
async def _bet_back_callback(callback: CallbackQuery) -> None:
    """Return to main nominations menu."""
    text = (
        "🎰 <b>VULTURE // ТОТАЛИЗАТОР СУДНОГО ДНЯ</b>\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "Выберите номинацию для ставки (при повторном выборе ставка перезаписывается):"
    )
    builder = _build_nominations_keyboard()
    try:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
        await callback.answer()
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("bet_cat:"))
async def _bet_cat_callback(callback: CallbackQuery) -> None:
    """Show top candidates for the chosen nomination."""
    if callback.from_user is None or callback.message is None:
        return

    category = callback.data.split(":", 1)[1]
    cat_name = CAT_TITLES.get(category, category)
    chat_id = callback.message.chat.id

    top_candidates = await get_top_active_users(chat_id, exclude_user_id=0, limit=6, days=7)
    if not top_candidates:
        try:
            await callback.answer("Недостаточно участников в базе для ставок.", show_alert=True)
        except TelegramAPIError:
            pass
        return

    builder = InlineKeyboardBuilder()
    for u in top_candidates:
        uname = u["username"]
        builder.button(text=f"@{uname}", callback_data=f"bet_pick:{category}:{uname}")
    builder.adjust(2)
    builder.row()
    builder.button(text="⬅️ Назад к номинациям", callback_data="bet_back")

    text = (
        f"🎰 <b>ТОТАЛИЗАТОР // ВЫБОР КАНДИДАТА</b>\n"
        f"Номинация: <b>{cat_name}</b>\n\n"
        f"Выберите участника из топа активности чата:\n"
        f"<i>(При повторном клике старая ставка будет перезаписана)</i>"
    )

    try:
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
        await callback.answer()
    except TelegramAPIError:
        pass


@router.callback_query(F.data.startswith("bet_pick:"))
async def _bet_pick_callback(callback: CallbackQuery) -> None:
    """Save or overwrite a user's bet."""
    if callback.from_user is None or callback.message is None:
        return

    try:
        _, category, target_username = callback.data.split(":", 2)
    except Exception:
        return

    chat_id = callback.message.chat.id
    voter_id = callback.from_user.id
    voter_username = _resolve_username(callback.from_user)
    week_key = datetime.now(timezone.utc).strftime("%Y-W%W")
    cat_name = CAT_TITLES.get(category, category)

    async with get_session() as session:
        res = await session.execute(
            select(PurgeBet).where(
                PurgeBet.chat_id == chat_id,
                PurgeBet.voter_id == voter_id,
                PurgeBet.category == category,
                PurgeBet.week_key == week_key,
            )
        )
        existing = res.scalar_one_or_none()
        if existing:
            existing.target_username = target_username
        else:
            bet = PurgeBet(
                chat_id=chat_id,
                voter_id=voter_id,
                voter_username=voter_username,
                category=category,
                target_username=target_username,
                week_key=week_key,
            )
            session.add(bet)
        await session.commit()

    try:
        await callback.answer("🎯 Ваша ставка в этой номинации успешно сохранена!", show_alert=True)
    except TelegramAPIError:
        pass

    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Выбрать другую номинацию", callback_data="bet_back")

    confirmation_text = (
        f"🎯 <b>Ваша ставка в этой номинации успешно сохранена!</b>\n\n"
        f"Номинация: <b>{cat_name}</b>\n"
        f"Ваш выбор: <b>@{html.escape(target_username)}</b>\n\n"
        f"<i>Вы можете изменить свой выбор в любой момент до воскресенья 20:00 МСК.</i>"
    )

    try:
        await callback.message.edit_text(confirmation_text, parse_mode=ParseMode.HTML, reply_markup=builder.as_markup())
    except TelegramAPIError:
        pass


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

        async with get_session() as session:
            res = await session.execute(select(Group).where(Group.chat_id == chat_id))
            grp = res.scalar_one_or_none()
            if grp is None or grp.is_initialized:
                return
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

    if has_reply:
        asyncio.create_task(_check_and_fire_calibration(bot, message.chat.id))

    # Event Interceptions (Trigger check: 2.5h cooldown, non-blocking)
    asyncio.create_task(maybe_fire_interception(bot, message.chat.id))


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

    if event.old_chat_member.status not in ("member", "administrator") and new_status in ("member", "administrator"):
        try:
            await bot.send_message(chat_id, WELCOME_MESSAGE, parse_mode=ParseMode.HTML)
        except TelegramAPIError:
            logger.debug("Failed to send welcome message to chat %s", chat_id)