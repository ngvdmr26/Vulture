"""Vulture async database layer – SQLAlchemy 2.0 ORM models & session setup."""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, String, ForeignKey
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

logger = logging.getLogger(__name__)

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
    joined_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_report_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, default=None)


class Message(Base):
    __tablename__ = "messages"

    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    chat_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("groups.chat_id"), primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(BigInteger, index=True)
    username: Mapped[str] = mapped_column(String(255), default="")
    reply_to_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, default=None)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    text_length: Mapped[int] = mapped_column(Integer, default=0)
    has_media: Mapped[bool] = mapped_column(Boolean, default=False)


class Reaction(Base):
    __tablename__ = "reactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, index=True)
    target_user_id: Mapped[int] = mapped_column(BigInteger)
    from_user_id: Mapped[int] = mapped_column(BigInteger)
    reaction_type: Mapped[str] = mapped_column(String(64), default="")
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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

    _engine = create_async_engine(url, echo=False, pool_pre_ping=True)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    logger.info("Database initialised – tables ready (%s)", url.split("://")[0])


def get_session() -> AsyncSession:
    """Return a fresh :class:`AsyncSession` from the factory.

    Usage::

        session = get_session()
        try:
            ...
        finally:
            await session.close()
    """
    if _session_factory is None:
        raise RuntimeError("Database not initialised – call init_db() first")
    return _session_factory()
