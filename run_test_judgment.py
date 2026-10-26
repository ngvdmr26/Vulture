#!/usr/bin/env python3
"""Vulture – Test script for Judgment Day & Clown of the Week mechanics.

Connects to /root/vulture/vulture.db (or fallback/specified DB),
executes weekly purge calculation (get_weekly_purge), inspects clown reactions (🤡),
determines the clown leader, assigns the status in UserGroupStats,
and prints a comprehensive console report.

Usage on server:
    python3 run_test_judgment.py
    python3 run_test_judgment.py --chat-id -100123456789
    python3 run_test_judgment.py --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import logging
import os
import sys

# Ensure UTF-8 output encoding for consoles that default to cp1251/ascii
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from sqlalchemy import func, select

from database import (
    Group,
    Message,
    Reaction,
    UserGroupStats,
    get_or_create_user_stats,
    get_session,
    init_db,
)
from engine.graph import get_weekly_purge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("test_judgment")

PRIMARY_DB_PATH = "/root/vulture/vulture.db"
LOCAL_FALLBACK_DB = "vulture.db"


async def test_judgment_for_group(
    chat_id: int,
    group_title: str,
    days: int = 7,
    dry_run: bool = False,
) -> None:
    """Analyze clown reactions and test Judgment Day clown title assignment for a single group."""
    separator = "=" * 64
    print(f"\n{separator}")
    print(f"📊 ГРУППА: {group_title} (Chat ID: {chat_id})")
    print(f"📅 Период анализа: последние {days} дней")
    print(separator)

    # --- 1. Real weekly purge calculation ---
    print("\n[1/3] Запуск еженедельного расчета get_weekly_purge()...")
    try:
        report = await get_weekly_purge(chat_id)
        if report:
            print("  • Серый кардинал:      ", report.get("top_influencer"))
            print("  • Клоун недели:        ", report.get("clown_of_the_week"))
            print("  • Главный игнор недели:", report.get("neglected"))
            print("  • Скрытый альянс:      ", report.get("secret_pair"))
        else:
            print("  ⚠️ get_weekly_purge() вернул None (мало взаимодействий в графе за 7 дней).")
    except Exception as exc:
        print(f"  ❌ Ошибка при вызове get_weekly_purge(): {exc}")

    # --- 2. Direct inspection of clown & toxic reactions ---
    print(f"\n[2/3] Детальный аудит реакций (🤡, 💩, 🤮)...")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cutoff = now - timedelta(days=days)

    async with get_session() as session:
        # User mapping for readable usernames
        res_users = await session.execute(
            select(Message.user_id, Message.username)
            .where(Message.chat_id == chat_id)
            .group_by(Message.user_id)
        )
        user_map: dict[int, str] = {r.user_id: r.username for r in res_users.all() if r.username}

        # Query recent toxic reactions
        res_toxic = await session.execute(
            select(
                Reaction.target_user_id,
                Reaction.from_user_id,
                Reaction.reaction_type,
                Reaction.timestamp,
            )
            .where(
                Reaction.chat_id == chat_id,
                Reaction.reaction_type.in_(["🤡", "💩", "🤮"]),
                Reaction.timestamp >= cutoff,
            )
            .order_by(Reaction.timestamp.desc())
        )
        recent_reactions = res_toxic.all()

        # Query total all-time toxic reactions
        res_all_time = await session.execute(
            select(func.count(Reaction.id))
            .where(
                Reaction.chat_id == chat_id,
                Reaction.reaction_type.in_(["🤡", "💩", "🤮"]),
            )
        )
        total_all_time = res_all_time.scalar_one() or 0

        # Query all-time 🤡 specifically
        res_pure_clown = await session.execute(
            select(func.count(Reaction.id))
            .where(
                Reaction.chat_id == chat_id,
                Reaction.reaction_type == "🤡",
                Reaction.timestamp >= cutoff,
            )
        )
        pure_clown_cutoff = res_pure_clown.scalar_one() or 0

    print(f"  • Найдено 🤡 реакций за последние {days} дн.: {pure_clown_cutoff}")
    print(f"  • Всего токсичных (🤡,💩,🤮) за {days} дн.: {len(recent_reactions)}")
    print(f"  • Всего токсичных реакций за всё время:      {total_all_time}")

    if not recent_reactions:
        print("\n  ℹ️ За последние 7 дней реакций 🤡 не зафиксировано.")
        print("  Титул «Клоун недели»: Никто не заслужил.")
        return

    clown_targets = [r.target_user_id for r in recent_reactions]
    target_counts: dict[int, int] = {}
    for uid in clown_targets:
        target_counts[uid] = target_counts.get(uid, 0) + 1

    sorted_targets = sorted(target_counts.items(), key=lambda x: x[1], reverse=True)

    print("\n  📋 Рейтинг получателей реакций 🤡:")
    for rank, (uid, cnt) in enumerate(sorted_targets, 1):
        uname = user_map.get(uid, f"id_{uid}")
        print(f"    #{rank} @{uname} (ID: {uid}) — {cnt} реакций")

    # --- 3. Determine winner & assign title ---
    top_clown_id, top_count = sorted_targets[0]
    top_clown_username = user_map.get(top_clown_id, f"id_{top_clown_id}")

    print("\n[3/3] Определение победителя и назначение статуса...")
    print(f"  🤡 Абсолютный лидер номинации: @{top_clown_username} ({top_count} реакций)")

    if dry_run:
        print("  [DRY-RUN] Флаг --dry-run активен: статус в БД не записывается.")
        return

    async with get_session() as session:
        stats = await get_or_create_user_stats(session, chat_id, top_clown_id, top_clown_username)
        stats.custom_status = "🤡 Клоун недели"
        stats.custom_status_by = "Судный день"
        stats.custom_status_until = now + timedelta(days=7)
        await session.commit()

        # Re-fetch to verify persistence
        res_v = await session.execute(
            select(UserGroupStats).where(
                UserGroupStats.chat_id == chat_id,
                UserGroupStats.user_id == top_clown_id,
            )
        )
        verified = res_v.scalar_one_or_none()

    if verified:
        print("  ✅ [УСПЕХ] Титул «Клоун недели» успешно зафиксирован в БД:")
        print(f"     • Пользователь:        @{verified.username or f'id_{verified.user_id}'}")
        print(f"     • custom_status:       «{verified.custom_status}»")
        print(f"     • custom_status_by:    «{verified.custom_status_by}»")
        print(f"     • custom_status_until: {verified.custom_status_until} UTC")
        print(f"     • Действует до следующего Судного дня (7 дней)")
    else:
        print("  ⚠️ Не удалось верифицировать запись в UserGroupStats.")


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Тестирование механики Судного дня: расчет и выдача титула «Клоун недели»",
    )
    parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Путь к SQLite базе (по умолчанию /root/vulture/vulture.db, либо ./vulture.db)",
    )
    parser.add_argument(
        "--chat-id",
        type=int,
        default=None,
        help="Конкретный chat_id группы для теста (если не указан — тестируются все группы)",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=7,
        help="Временное окно подсчета реакций в днях (по умолчанию 7)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Только расчет без записи статуса в БД",
    )
    parser.add_argument(
        "--mock",
        action="store_true",
        help="Внедрить 1 тестовую реакцию 🤡 для проверки, если база чистая",
    )
    args = parser.parse_args()

    # Determine database path
    if args.db:
        db_path = args.db
    elif os.path.exists(PRIMARY_DB_PATH):
        db_path = PRIMARY_DB_PATH
    else:
        db_path = LOCAL_FALLBACK_DB

    print("════════════════════════════════════════════════════════════════")
    print(" 🦅 VULTURE // ТЕСТ СУДНОГО ДНЯ: ТИТУЛ «КЛОУН НЕДЕЛИ»")
    print("════════════════════════════════════════════════════════════════")
    print(f"База данных: {db_path}")

    db_url = f"sqlite+aiosqlite:///{db_path}"
    await init_db(db_url)

    # --- DB Diagnostics ---
    async with get_session() as session:
        msg_count = (await session.execute(select(func.count(Message.message_id)))).scalar_one() or 0
        rxn_count = (await session.execute(select(func.count(Reaction.id)))).scalar_one() or 0
        grp_count = (await session.execute(select(func.count(Group.chat_id)))).scalar_one() or 0

        res_latest_msgs = await session.execute(
            select(Message.message_id, Message.chat_id, Message.username, Message.timestamp)
            .order_by(Message.timestamp.desc())
            .limit(3)
        )
        latest_msgs = res_latest_msgs.all()

    print(f"\n📈 СТАТУС БАЗЫ ДАННЫХ:")
    print(f"  • Всего групп:      {grp_count}")
    print(f"  • Всего сообщений:  {msg_count}")
    print(f"  • Всего реакций:    {rxn_count}")

    if msg_count == 0:
        print("\n  ⚠️ ВНИМАНИЕ: В таблице 'messages' ровно 0 сообщений!")
        print("  Telegram Bot API не сообщает автора сообщения в событии реакции.")
        print("  Бот берет автора из таблицы 'messages'. Если сообщение не было")
        print("  записано ботом, реакция игнорируется.")
        print("  👉 Причина: Включен Group Privacy Mode в @BotFather.")
        print("     Решение: Перейдите в @BotFather -> /setprivacy -> выберите бота -> Disable.")
    elif latest_msgs:
        print("  • Последние зафиксированные сообщения:")
        for m in latest_msgs:
            print(f"    - msg_id={m.message_id} | @{m.username or 'unknown'} | {m.timestamp} UTC")

    # Discover groups to analyze
    target_groups: list[tuple[int, str]] = []
    async with get_session() as session:
        if args.chat_id:
            res_grp = await session.execute(
                select(Group).where(Group.chat_id == args.chat_id)
            )
            g = res_grp.scalar_one_or_none()
            target_groups.append((args.chat_id, g.title if g else "Указанная группа"))
        else:
            res_grps = await session.execute(select(Group))
            db_groups = res_grps.scalars().all()
            for g in db_groups:
                target_groups.append((g.chat_id, g.title or f"Chat {g.chat_id}"))

            # Also check reactions/messages table if groups table is empty
            if not target_groups:
                res_chats = await session.execute(select(Reaction.chat_id).distinct())
                reaction_chats = res_chats.scalars().all()
                for cid in reaction_chats:
                    target_groups.append((cid, f"Chat from reactions {cid}"))

    if not target_groups:
        print("\n⚠️ В базе данных пока нет зарегистрированных групп.")
        if args.mock:
            mock_chat_id = -1009999999999
            print(f"Создаем мок-группу {mock_chat_id} и тестовые реакции 🤡...")
            async with get_session() as session:
                session.add(Group(chat_id=mock_chat_id, title="Test Group Mock", is_active=True))
                session.add(
                    Reaction(
                        chat_id=mock_chat_id,
                        target_user_id=111,
                        from_user_id=222,
                        reaction_type="🤡",
                    )
                )
                session.add(
                    Reaction(
                        chat_id=mock_chat_id,
                        target_user_id=111,
                        from_user_id=333,
                        reaction_type="🤡",
                    )
                )
                session.add(
                    Message(
                        message_id=1,
                        chat_id=mock_chat_id,
                        user_id=111,
                        username="test_clown_target",
                    )
                )
                await session.commit()
            target_groups.append((mock_chat_id, "Test Group Mock"))
        else:
            print("Используйте параметр --mock для генерации тестовых данных при пустой БД,")
            print("либо укажите --chat-id <ID>.")
            return

    for cid, title in target_groups:
        await test_judgment_for_group(
            chat_id=cid,
            group_title=title,
            days=args.days,
            dry_run=args.dry_run,
        )

    print("\n════════════════════════════════════════════════════════════════")
    print(" ✅ Тестирование завершено.")
    print("════════════════════════════════════════════════════════════════\n")


if __name__ == "__main__":
    asyncio.run(main())
