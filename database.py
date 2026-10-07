"""Vulture async database layer – SQLAlchemy 2.0 ORM models & session setup."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import BigInteger, Boolean, DateTime, ForeignKey, Index, Integer, String, text
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

    logger.info("Database initialised – tables ready (%s)", url.split("://")[0])


def get_session() -> AsyncSession:
    """Return a fresh :class:`AsyncSession` from the factory.

    Usage::

        async with get_session() as session:
            ...
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised – call init_db() first")
    return _session_factory()
