"""Schema migration tests.

The bug these guard against is subtle and was only caught by running the real server:
`create_all` creates missing tables but never alters an existing one, so every test
against a fresh database passed while the actual database kept failing at insert time.
The regression test therefore has to build a database with the *old* schema first.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.migrations import apply_additive_migrations


LEGACY_CHUNKS_DDL = """
CREATE TABLE chunks (
    id VARCHAR(32) PRIMARY KEY,
    workspace_id VARCHAR(32) NOT NULL,
    file_id VARCHAR(32) NOT NULL,
    ordinal INTEGER NOT NULL,
    text TEXT NOT NULL,
    location VARCHAR(300) NOT NULL,
    meta JSON NOT NULL,
    token_counts JSON NOT NULL,
    token_length INTEGER NOT NULL,
    created_at DATETIME NOT NULL
)
"""


def _legacy_engine(tmp_path: Path):
    engine = create_engine(f"sqlite:///{(tmp_path / 'legacy.db').as_posix()}", future=True)
    with engine.begin() as connection:
        connection.execute(text(LEGACY_CHUNKS_DDL))
        connection.execute(
            text(
                "INSERT INTO chunks (id, workspace_id, file_id, ordinal, text, location,"
                " meta, token_counts, token_length, created_at)"
                " VALUES ('c1','w1','f1',0,'旧数据','段落 1','{}','{}',1,'2026-01-01')"
            )
        )
    return engine


def test_migration_adds_missing_columns(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path)
    before = {c["name"] for c in inspect(engine).get_columns("chunks")}
    assert "level" not in before and "parent_id" not in before

    apply_additive_migrations(engine)

    after = {c["name"] for c in inspect(engine).get_columns("chunks")}
    assert {"level", "parent_id"} <= after
    engine.dispose()


def test_migration_preserves_existing_rows(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path)
    apply_additive_migrations(engine)

    with engine.begin() as connection:
        row = connection.execute(
            text("SELECT text, level, parent_id FROM chunks WHERE id='c1'")
        ).one()

    # Content survives, and the backfilled level keeps old rows retrievable instead of
    # silently dropping them out of search.
    assert row[0] == "旧数据"
    assert row[1] == "child"
    assert row[2] is None
    engine.dispose()


def test_migration_is_idempotent(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path)
    first = apply_additive_migrations(engine)
    second = apply_additive_migrations(engine)

    assert first  # something was applied the first time
    assert second == []  # and nothing the second time
    engine.dispose()


def test_migration_skips_a_fresh_database(tmp_path: Path) -> None:
    """A database created by the current code needs no patching."""
    from app.db import Base
    from app import models  # noqa: F401  (register mappers)

    engine = create_engine(f"sqlite:///{(tmp_path / 'fresh.db').as_posix()}", future=True)
    Base.metadata.create_all(engine)

    assert apply_additive_migrations(engine) == []
    engine.dispose()


def test_migration_creates_indexes(tmp_path: Path) -> None:
    engine = _legacy_engine(tmp_path)
    apply_additive_migrations(engine)
    indexes = {index["name"] for index in inspect(engine).get_indexes("chunks")}
    assert {"ix_chunks_parent_id", "ix_chunk_level_workspace"} <= indexes
    engine.dispose()
