"""Schema migration tests.

The bug these guard against is subtle and was only caught by running the real server:
`create_all` creates missing tables but never alters an existing one, so every test
against a fresh database passed while the actual database kept failing at insert time.
The regression test therefore has to build a database with the *old* schema first.
"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.migrations import (
    apply_additive_migrations,
    dedupe_meeting_segments,
    normalize_legacy_timestamps,
)


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


def test_news_articles_get_time_basis_column(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'news-legacy.db').as_posix()}", future=True
    )
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE news_articles (id VARCHAR(32) PRIMARY KEY)")
        )

    assert "news_articles.time_basis" in apply_additive_migrations(engine)
    columns = {item["name"] for item in inspect(engine).get_columns("news_articles")}
    assert "time_basis" in columns
    engine.dispose()


LEGACY_WORKSPACES_DDL = """
CREATE TABLE workspaces (
    id VARCHAR(32) PRIMARY KEY,
    name VARCHAR(200) NOT NULL,
    description TEXT,
    created_at DATETIME NOT NULL,
    updated_at DATETIME NOT NULL
)
"""


def _legacy_timestamp_engine(tmp_path: Path):
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'mixed-times.db').as_posix()}", future=True
    )
    with engine.begin() as connection:
        connection.execute(text(LEGACY_WORKSPACES_DDL))
        connection.execute(
            text(
                "INSERT INTO workspaces (id,name,description,created_at,updated_at)"
                " VALUES ('java','Java','','1789059716722','1789059737724')"
            )
        )
        connection.execute(
            text(
                "INSERT INTO workspaces (id,name,description,created_at,updated_at)"
                " VALUES ('python','Python','','2026-09-11 12:37:26.079108',"
                "'2026-09-11 13:17:16.962499')"
            )
        )
    return engine


def test_timestamp_normalization_is_mixed_type_safe(tmp_path: Path) -> None:
    engine = _legacy_timestamp_engine(tmp_path)
    result = normalize_legacy_timestamps(engine)
    assert any("normalized timestamp values" in item for item in result)

    with engine.connect() as connection:
        kinds = connection.execute(
            text("SELECT typeof(created_at), count(*) FROM workspaces GROUP BY 1")
        ).fetchall()
    assert kinds == [("integer", 2)]

    from app.models import Workspace

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        rows = session.query(Workspace).order_by(Workspace.created_at).all()
        assert [row.id for row in rows] == ["java", "python"]
        assert rows[0].created_at.isoformat() == "2026-09-10T17:01:56.722000+00:00"
        # SQLite now stores epoch milliseconds, matching the Java rows.
        assert rows[1].created_at.isoformat() == "2026-09-11T12:37:26.079000+00:00"
    finally:
        session.close()

    backups = list(tmp_path.glob("mixed-times.db.pre-epoch-normalize-*.bak"))
    assert len(backups) == 1
    engine.dispose()


def test_model_can_read_iso_string_even_before_migration(tmp_path: Path) -> None:
    """The custom type is a second line of defence if a row is still text."""
    engine = _legacy_timestamp_engine(tmp_path)
    from app.models import Workspace

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        row = session.get(Workspace, "python")
        assert row is not None
        assert row.created_at.isoformat() == "2026-09-11T12:37:26.079108+00:00"
    finally:
        session.close()
    engine.dispose()


def test_timestamp_normalization_is_idempotent(tmp_path: Path) -> None:
    engine = _legacy_timestamp_engine(tmp_path)
    first = normalize_legacy_timestamps(engine)
    second = normalize_legacy_timestamps(engine)
    assert first
    assert second == []
    assert len(list(tmp_path.glob("mixed-times.db.pre-epoch-normalize-*.bak"))) == 1
    engine.dispose()


def test_new_python_timestamp_is_written_as_epoch_millis(tmp_path: Path) -> None:
    engine = _legacy_timestamp_engine(tmp_path)
    normalize_legacy_timestamps(engine)

    from app.models import Workspace

    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    session = factory()
    try:
        session.add(Workspace(name="new"))
        session.commit()
    finally:
        session.close()

    with engine.connect() as connection:
        kind = connection.execute(
            text("SELECT typeof(created_at) FROM workspaces WHERE name='new'")
        ).scalar_one()
    assert kind == "integer"
    engine.dispose()


MEETING_TRANSCRIPT_DDL = """
CREATE TABLE meetings (
    id VARCHAR(32) PRIMARY KEY,
    language VARCHAR(10) NOT NULL
);
CREATE TABLE meeting_segments (
    id VARCHAR(32) PRIMARY KEY,
    meeting_id VARCHAR(32) NOT NULL,
    ordinal INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    text TEXT NOT NULL,
    token_counts JSON NOT NULL,
    token_length INTEGER NOT NULL
);
"""


def test_meeting_segment_cleanup_dedupes_and_simplifies(tmp_path: Path) -> None:
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'meeting-wrap.db').as_posix()}", future=True
    )
    with engine.begin() as connection:
        for statement in MEETING_TRANSCRIPT_DDL.split(";"):
            if statement.strip():
                connection.execute(text(statement))
        connection.execute(text("INSERT INTO meetings (id, language) VALUES ('m1', 'zh')"))
        connection.execute(
            text(
                "INSERT INTO meeting_segments"
                " (id, meeting_id, ordinal, start_ms, end_ms, text, token_counts, token_length)"
                " VALUES ('s1','m1',0,1000,2000,'這是','{}',1),"
                " ('s2','m1',1,1000,4000,'這是繁體中文','{}',5),"
                " ('s3','m1',2,5000,6000,'字幕by索兰娅','{}',4),"
                " ('s4','m1',3,8000,9000,'保留这句','{}',3),"
                " ('s5','m1',4,10000,11000,"
                "'讨论结束请使用简体中文普通话会议，','{}',12)"
            )
        )

    result = dedupe_meeting_segments(engine)
    assert any("2 stale/hallucinated row" in item for item in result)
    assert list(tmp_path.glob("meeting-wrap.db.pre-meeting-dedupe-*.bak"))

    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT start_ms, end_ms, text, ordinal"
                " FROM meeting_segments ORDER BY start_ms"
            )
        ).fetchall()
    assert rows == [
        (1000, 4000, "这是繁体中文", 0),
        (8000, 9000, "保留这句", 1),
        (10000, 11000, "讨论结束", 2),
    ]
    indexes = {item["name"] for item in inspect(engine).get_indexes("meeting_segments")}
    assert "uq_meeting_segment_start" in indexes
    assert dedupe_meeting_segments(engine) == []
    engine.dispose()
