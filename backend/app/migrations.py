"""Lightweight additive schema migrations.

Why this exists rather than Alembic: this is a single-user local application whose
database lives in the project directory, and the only schema changes so far have been
additive columns. A full migration framework would add a dependency, a config surface,
and a versioning scheme to solve a problem that is currently a few ``ALTER TABLE``
statements.

The rule this module enforces is that migrations are **additive only** — no column is
dropped, no table is rewritten, no data is touched. That is what makes it safe to run
automatically on every startup without asking the operator.

The bug this was written for is worth recording: `create_all` only creates *missing
tables*. It never alters an existing one. So a developer adding a column sees every
test pass against a fresh database while the real database — the one that already had
a `chunks` table — keeps failing at insert time with "no such column".
"""

from __future__ import annotations

import logging

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# table -> (column, DDL type, default literal or None)
_ADDITIVE_COLUMNS: dict[str, list[tuple[str, str, str | None]]] = {
    "chunks": [
        # Pre-hierarchy rows are flat and were all retrievable, so "child" is the
        # correct backfill: it preserves their existing behaviour rather than
        # silently dropping them out of retrieval.
        ("level", "VARCHAR(10)", "'child'"),
        ("parent_id", "VARCHAR(32)", None),
    ],
}

_INDEXES: list[tuple[str, str, str]] = [
    ("chunks", "ix_chunks_parent_id", "parent_id"),
    ("chunks", "ix_chunk_level_workspace", "level, workspace_id"),
]


def _existing_columns(engine: Engine, table: str) -> set[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {column["name"] for column in inspector.get_columns(table)}


def _existing_indexes(engine: Engine, table: str) -> set[str]:
    inspector = inspect(engine)
    if table not in inspector.get_table_names():
        return set()
    return {index["name"] for index in inspector.get_indexes(table)}


def apply_additive_migrations(engine: Engine) -> list[str]:
    """Add any missing columns and indexes. Returns a log of what was applied."""

    applied: list[str] = []

    for table, columns in _ADDITIVE_COLUMNS.items():
        present = _existing_columns(engine, table)
        if not present:
            # The table does not exist yet; `create_all` will build it correctly.
            continue

        for column, column_type, default in columns:
            if column in present:
                continue
            clause = f"ALTER TABLE {table} ADD COLUMN {column} {column_type}"
            if default is not None:
                clause += f" DEFAULT {default}"
            with engine.begin() as connection:
                connection.execute(text(clause))
            applied.append(f"{table}.{column}")
            logger.info("migration: added column %s.%s", table, column)

    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    for table, index_name, columns in _INDEXES:
        if table not in existing_tables:
            continue
        if index_name in _existing_indexes(engine, table):
            continue
        with engine.begin() as connection:
            connection.execute(
                text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table} ({columns})")
            )
        applied.append(index_name)
        logger.info("migration: created index %s", index_name)

    return applied
