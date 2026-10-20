"""Lightweight schema and compatibility migrations.

Why this exists rather than Alembic: this is a single-user local application whose
database lives in the project directory, and the only schema changes so far have been
additive columns. A full migration framework would add a dependency, a config surface,
and a versioning scheme to solve a problem that is currently a few ``ALTER TABLE``
statements.

Schema changes remain additive: no column is dropped and no table is rewritten.
There is one explicit data migration, ``normalize_legacy_timestamps``, because the
database was previously shared with a Java backend that stored timestamps as epoch
milliseconds while Python wrote ISO strings. It takes a SQLite backup before
touching any row, converts only text/real timestamp values to epoch milliseconds,
and is idempotent.

The bug this was written for is worth recording: `create_all` only creates *missing
tables*. It never alters an existing one. So a developer adding a column sees every
test pass against a fresh database while the real database — the one that already had
a `chunks` table — keeps failing at insert time with "no such column".
"""

from __future__ import annotations

import logging
import json
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from .db_types import to_epoch_millis

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
    "messages": [
        # Thumbs feedback on assistant turns; NULL keeps pre-feedback rows valid.
        ("feedback", "VARCHAR(10)", None),
    ],
}

_INDEXES: list[tuple[str, str, str]] = [
    ("chunks", "ix_chunks_parent_id", "parent_id"),
    ("chunks", "ix_chunk_level_workspace", "level, workspace_id"),
]

# Python-owned tables. Java-only tables (agent_plans, operation_batches,
# plan_steps) are intentionally excluded so normalizing Python data does not
# break a Java process that may still expect epoch values there.
_DATETIME_COLUMNS: dict[str, tuple[str, ...]] = {
    "workspaces": ("created_at", "updated_at"),
    "document_files": ("indexed_at", "created_at"),
    "chunks": ("created_at",),
    "messages": ("created_at",),
    "conversation_summaries": ("updated_at",),
    "run_traces": ("created_at",),
    "operations": ("created_at", "resolved_at"),
    "mcp_tools": ("updated_at",),
    "assistant_conversations": ("created_at", "updated_at"),
    "assistant_messages": ("created_at",),
    "assistant_actions": ("created_at", "expires_at", "resolved_at"),
    "meetings": ("started_at", "ended_at", "created_at", "updated_at"),
    "meeting_segments": ("created_at",),
}


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


def _backup_sqlite_database(
    engine: Engine, *, label: str = "epoch-normalize"
) -> str | None:
    """Create a consistent sidecar backup before a data migration."""
    database = engine.url.database
    if not database or database == ":memory:":
        return None
    source_path = Path(database).resolve()
    if not source_path.exists():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = source_path.with_name(f"{source_path.name}.pre-{label}-{stamp}.bak")
    source = sqlite3.connect(str(source_path))
    target = sqlite3.connect(str(backup_path))
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    return str(backup_path)


def normalize_legacy_timestamps(engine: Engine) -> list[str]:
    """Convert legacy text ISO timestamps to epoch milliseconds.

    Integer epoch rows are already in the target representation. The update is
    performed only after every value parses, and a database backup is written
    first. A second run is a no-op.
    """
    if engine.dialect.name != "sqlite":
        return []

    pending: list[tuple[str, str, int, int]] = []
    with engine.connect() as connection:
        for table, columns in _DATETIME_COLUMNS.items():
            present = _existing_columns(engine, table)
            if not present:
                continue
            for column in columns:
                if column not in present:
                    continue
                rows = connection.exec_driver_sql(
                    f'SELECT rowid, "{column}" FROM "{table}" '
                    f'WHERE typeof("{column}") IN (\'text\', \'real\')'
                ).fetchall()
                for rowid, value in rows:
                    try:
                        millis = to_epoch_millis(value)
                    except Exception as exc:
                        raise ValueError(
                            f"cannot normalize {table}.{column} rowid={rowid}: {exc}"
                        ) from exc
                    pending.append((table, column, int(rowid), millis))

    if not pending:
        return []

    backup = _backup_sqlite_database(engine)
    with engine.begin() as connection:
        for table, column, rowid, millis in pending:
            connection.exec_driver_sql(
                f'UPDATE "{table}" SET "{column}" = ? WHERE rowid = ?',
                (millis, rowid),
            )

    counts: dict[str, int] = {}
    for table, column, _, _ in pending:
        key = f"{table}.{column}"
        counts[key] = counts.get(key, 0) + 1
    explanation = ", ".join(f"{key} ({count})" for key, count in counts.items())
    result = [f"normalized timestamp values: {explanation}"]
    if backup:
        result.append(f"backup: {backup}")
    logger.info("migration: %s", "; ".join(result))
    return result


def dedupe_meeting_segments(engine: Engine) -> list[str]:
    """Collapse cumulative ASR snapshots and normalize existing transcripts.

    Earlier versions keyed lines by ``(start_ms, end_ms)``. A growing utterance
    therefore produced one row for every revision. This migration keeps the
    longest/latest revision per start, applies the same Simplified-Chinese and
    hallucination rules as the live path, backs up SQLite before changing data,
    and then installs the stable-start unique index.
    """
    if engine.dialect.name != "sqlite":
        return []
    if not _existing_columns(engine, "meeting_segments"):
        return []

    from .meeting.text import is_probable_hallucination, normalize_transcript_text
    from .retrieval.bm25 import tokenize
    from .services.token_budget import estimate_tokens

    with engine.connect() as connection:
        rows = [
            dict(row)
            for row in connection.exec_driver_sql(
                """
                SELECT s.rowid AS rowid, s.id AS id, s.meeting_id AS meeting_id,
                       s.start_ms AS start_ms, s.end_ms AS end_ms,
                       s.text AS text, s.ordinal AS ordinal,
                       m.language AS language
                FROM meeting_segments s
                JOIN meetings m ON m.id = s.meeting_id
                ORDER BY s.meeting_id, s.start_ms, s.end_ms
                """
            ).mappings()
        ]

    grouped: dict[tuple[str, int], list[dict]] = defaultdict(list)
    for row in rows:
        grouped[(row["meeting_id"], int(row["start_ms"]))].append(row)

    deletes: list[int] = []
    updates: list[tuple[int, str, str, int]] = []
    affected_meetings: set[str] = set()
    normalized_count = 0
    removed_count = 0

    for (_meeting_id, _start_ms), candidates in grouped.items():
        candidates.sort(
            key=lambda row: (
                int(row["end_ms"]),
                len(row["text"] or ""),
                int(row["rowid"]),
            ),
            reverse=True,
        )
        keep = candidates[0]
        for duplicate in candidates[1:]:
            deletes.append(int(duplicate["rowid"]))
            removed_count += 1
            affected_meetings.add(str(duplicate["meeting_id"]))

        text = normalize_transcript_text(
            str(keep["text"] or ""),
            language=str(keep["language"] or "zh"),
            simplify=True,
        )
        if not text or is_probable_hallucination(
            text,
            start_ms=int(keep["start_ms"]),
            end_ms=int(keep["end_ms"]),
        ):
            deletes.append(int(keep["rowid"]))
            removed_count += 1
            affected_meetings.add(str(keep["meeting_id"]))
            continue

        counts = dict(Counter(tokenize(text)))
        serialized_counts = json.dumps(counts, ensure_ascii=False)
        if text != str(keep["text"] or ""):
            updates.append(
                (
                    int(keep["rowid"]),
                    text,
                    serialized_counts,
                    estimate_tokens(text),
                )
            )
            normalized_count += 1
            affected_meetings.add(str(keep["meeting_id"]))

    inspector = inspect(engine)
    index_names = (
        {index["name"] for index in inspector.get_indexes("meeting_segments")}
        if "meeting_segments" in inspector.get_table_names()
        else set()
    )
    need_index = "uq_meeting_segment_start" not in index_names
    if not deletes and not updates and not need_index:
        return []

    backup = (
        _backup_sqlite_database(engine, label="meeting-dedupe")
        if (deletes or updates)
        else None
    )
    with engine.begin() as connection:
        for rowid in deletes:
            connection.exec_driver_sql(
                "DELETE FROM meeting_segments WHERE rowid = ?", (rowid,)
            )
        for rowid, text, counts, length in updates:
            connection.exec_driver_sql(
                """
                UPDATE meeting_segments
                SET text = ?, token_counts = ?, token_length = ?
                WHERE rowid = ?
                """,
                (text, counts, length, rowid),
            )

        for meeting_id in affected_meetings:
            ordinals = connection.exec_driver_sql(
                """
                SELECT rowid
                FROM meeting_segments
                WHERE meeting_id = ?
                ORDER BY start_ms, end_ms
                """,
                (meeting_id,),
            ).fetchall()
            for ordinal, (rowid,) in enumerate(ordinals):
                connection.exec_driver_sql(
                    "UPDATE meeting_segments SET ordinal = ? WHERE rowid = ?",
                    (ordinal, rowid),
                )

        connection.exec_driver_sql(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS uq_meeting_segment_start
            ON meeting_segments (meeting_id, start_ms)
            """
        )

    result = [
        "meeting transcript cleanup: "
        f"{removed_count} stale/hallucinated row(s) removed, "
        f"{normalized_count} row(s) simplified"
    ]
    if backup:
        result.append(f"backup: {backup}")
    logger.info("migration: %s", "; ".join(result))
    return result
