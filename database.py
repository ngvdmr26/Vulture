"""Vulture async database layer – SQLAlchemy 2.0 ORM models & session setup."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    select,
    text,
)
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# ---------------------------------------------------------------------------
# ORM Base
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------

class Group(Base):
    __tablename__ = "groups"

    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_initialized: Mapped[bool] = mapped_column(Boolean, default=False)
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_messages_chat_timestamp", "chat_id", "timestamp"),
        Index("ix_messages_chat_username", "chat_id", "username"),
    )

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.chat_id"), primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(255), default="")
    reply_to_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    text_length: Mapped[int] = mapped_column(Integer, default=0)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)


class Reaction(Base):
    __tablename__ = "reactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_user_id: Mapped[int] = mapped_column(BigInteger)
    from_user_id: Mapped[int] = mapped_column(BigInteger)
    reaction_type: Mapped[str] = mapped_column(String(64), default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class UserGroupStats(Base):
    """Stores charges, buffs, and branded statuses per user per group."""
    __tablename__ = "user_group_stats"
    __table_args__ = (
        Index("ix_user_group_stats_chat_user", "chat_id", "user_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    jammer_charges: Mapped[int] = mapped_column(Integer, default=1)
    last_jammer_refill: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    brand_charges: Mapped[int] = mapped_column(Integer, default=0)
    custom_status: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    custom_status_by: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    custom_status_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)
    bonus_influence: Mapped[float] = mapped_column(Float, default=0.0)
    correct_guesses_count: Mapped[int] = mapped_column(Integer, default=0)
    last_dossier_game_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class PurgeBet(Base):
    """Totalizator bets on weekly Purge Day outcomes."""
    __tablename__ = "purge_bets"
    __table_args__ = (
        Index("ix_purge_bets_chat_week", "chat_id", "week_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    voter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    voter_username: Mapped[str | None] = mapped_column(String(255), nullable=True, default=None)
    category: Mapped[str] = mapped_column(String(64))  # 'top_influencer', 'clown_of_the_week', 'neglected'
    target_username: Mapped[str] = mapped_column(String(255))
    week_key: Mapped[str] = mapped_column(String(20), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class DossierGame(Base):
    """Active public dossier detector game session in a group."""
    __tablename__ = "dossier_games"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    subject_user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    subject_username: Mapped[str] = mapped_column(String(255))
    correct_answer: Mapped[str] = mapped_column(String(255))
    message_id: Mapped[int] = mapped_column(BigInteger)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    vote_count: Mapped[int] = mapped_column(Integer, default=0)
    threshold: Mapped[int] = mapped_column(Integer, default=5)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


class DossierGameVote(Base):
    """Votes cast in a dossier detector game."""
    __tablename__ = "dossier_game_votes"
    __table_args__ = (
        Index("ix_dgv_game_voter", "game_id", "voter_id", unique=True),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    game_id: Mapped[int] = mapped_column(Integer, ForeignKey("dossier_games.id"), index=True)
    voter_id: Mapped[int] = mapped_column(BigInteger, index=True)
    voted_for: Mapped[str] = mapped_column(String(255))
    is_correct: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)


# ---------------------------------------------------------------------------
# Engine & session factory (module-level singletons)
# ---------------------------------------------------------------------------

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


async def init_db(url: str | None = None) -> None:
    """Create engine, run ``CREATE TABLE`` for every model, set session factory."""
    global _engine, _session_factory

    if url is None:
        from config import get_settings
        url = get_settings().DATABASE_URL

    connect_args: dict = {}
    if "sqlite" in url:
        connect_args = {"timeout": 30}

    _engine = create_async_engine(
        url,
        echo=False,
        pool_pre_ping=True,
        connect_args=connect_args,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if "sqlite" in url:
            await conn.execute(text("PRAGMA journal_mode=WAL"))
            await conn.execute(text("PRAGMA busy_timeout=30000"))
            # Safe column migrations for existing databases
            migrations = [
                "ALTER TABLE groups ADD COLUMN is_initialized BOOLEAN NOT NULL DEFAULT 0",
            ]
            for sql in migrations:
                try:
                    await conn.execute(text(sql))
                    logger.info("Migration applied: %s", sql[:60])
                except Exception:
                    pass  # Column already exists

    logger.info("Database initialised – tables ready (%s)", url.split("://")[0])


def get_session() -> AsyncSession:
    """Return a fresh :class:`AsyncSession` from the factory."""
    if _session_factory is None:
        raise RuntimeError("Database not initialised – call init_db() first")
    return _session_factory()


async def get_or_create_user_stats(
    session: AsyncSession,
    chat_id: int,
    user_id: int,
    username: str | None = None,
) -> UserGroupStats:
    """Retrieve or create a UserGroupStats record and handle daily jammer auto-refill."""
    res = await session.execute(
        select(UserGroupStats).where(
            UserGroupStats.chat_id == chat_id,
            UserGroupStats.user_id == user_id,
        )
    )
    stats = res.scalar_one_or_none()
    now = _utcnow()

    if stats is None:
        stats = UserGroupStats(
            chat_id=chat_id,
            user_id=user_id,
            username=username,
            jammer_charges=1,
            last_jammer_refill=now,
        )
        session.add(stats)
        await session.flush()
        return stats

    if username and stats.username != username:
        stats.username = username

    # Auto-refill jammer up to 1 free charge every 24 hours
    if (stats.jammer_charges or 0) < 1:
        if stats.last_jammer_refill is None or (now - stats.last_jammer_refill) >= timedelta(hours=24):
            stats.jammer_charges = 1
            stats.last_jammer_refill = now

    return stats
