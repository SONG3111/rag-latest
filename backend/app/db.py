"""Database engine and session management (SQLite + SQLAlchemy 2.0)."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.pool import StaticPool

from .config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """Declarative base shared by every ORM model."""


settings = get_settings()

_connect_args: dict = {}
_engine_kwargs: dict = {}
if settings.effective_database_url.startswith("sqlite"):
    _connect_args["check_same_thread"] = False
    if ":memory:" in settings.effective_database_url:
        _engine_kwargs["poolclass"] = StaticPool

engine = create_engine(
    settings.effective_database_url,
    connect_args=_connect_args,
    future=True,
    **_engine_kwargs,
)


@event.listens_for(engine, "connect")
def _configure_sqlite(dbapi_connection, _connection_record) -> None:  # pragma: no cover
    """Enable WAL and foreign keys so concurrent reads behave predictably."""
    if not settings.effective_database_url.startswith("sqlite"):
        return
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    # SQLite allows a single writer; without a generous busy timeout a writer
    # (e.g. an approval click) fails with ``database is locked`` instead of
    # briefly queuing behind another short write transaction.
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create missing tables, then bring an existing database up to the current schema.

    Order matters: ``create_all`` runs first so a brand-new database is built with the
    full schema, and the additive migrations then patch databases created by an older
    version of the code.
    """
    from . import models  # noqa: F401  (side effect: register mappers)
    from .migrations import apply_additive_migrations

    Base.metadata.create_all(bind=engine)
    applied = apply_additive_migrations(engine)
    if applied:
        logger.info("applied %d schema migration(s): %s", len(applied), ", ".join(applied))


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a scoped session."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context manager for background tasks and scripts."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
