"""Workspace file ingestion and knowledge-base indexing."""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import Chunk, DocumentFile, IndexStatus, Workspace
from ..retrieval.bm25 import token_counts
from ..retrieval.chunking import chunk_document_groups
from ..retrieval.vector_store import VectorStore, VectorStoreError

logger = logging.getLogger(__name__)

SUPPORTED_SUFFIXES = {".xlsx", ".xlsm", ".docx"}
MAX_UPLOAD_BYTES = 200 * 1024 * 1024
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class IngestionError(RuntimeError):
    """Raised when an uploaded file cannot be accepted or parsed."""


@dataclass
class IndexingResult:
    file_id: str
    rel_path: str
    chunk_count: int
    vector_count: int
    status: IndexStatus
    error: str | None = None


def sanitize_filename(name: str) -> str:
    """Reduce a client-supplied filename to a safe basename.

    Browsers send whatever the user's filesystem contained, including path
    separators on some platforms, so the value is never trusted as a path.
    """
    base = Path(name or "").name
    base = unicodedata.normalize("NFKC", base).strip()
    base = _UNSAFE_CHARS.sub("_", base)
    base = base.strip(". ")
    if not base:
        raise IngestionError("filename is empty after sanitization")
    return base[:200]


def _unique_rel_path(workspace_dir: Path, filename: str) -> str:
    """Avoid clobbering an existing upload by suffixing ``(1)``, ``(2)``, ..."""
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    candidate = filename
    counter = 1
    while (workspace_dir / candidate).exists():
        candidate = f"{stem}({counter}){suffix}"
        counter += 1
    return candidate


def save_upload(
    session: Session,
    workspace: Workspace,
    filename: str,
    content: bytes,
    settings: Settings | None = None,
) -> DocumentFile:
    """Persist an uploaded document and register it as pending indexing."""
    settings = settings or get_settings()
    if len(content) > MAX_UPLOAD_BYTES:
        raise IngestionError(
            f"file exceeds the {MAX_UPLOAD_BYTES // (1024 * 1024)}MB upload limit"
        )
    if not content:
        raise IngestionError("uploaded file is empty")

    safe_name = sanitize_filename(filename)
    suffix = Path(safe_name).suffix.lower()
    if suffix not in SUPPORTED_SUFFIXES:
        raise IngestionError(
            f"unsupported file type '{suffix or '<none>'}'; "
            f"supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )

    workspace_dir = settings.workspace_dir(workspace.id)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    rel_path = _unique_rel_path(workspace_dir, safe_name)
    target = workspace_dir / rel_path
    target.write_bytes(content)

    record = DocumentFile(
        workspace_id=workspace.id,
        rel_path=rel_path,
        kind="excel" if suffix in {".xlsx", ".xlsm"} else "word",
        size_bytes=len(content),
        checksum=hashlib.sha256(content).hexdigest(),
        status=IndexStatus.pending,
    )
    session.add(record)
    session.flush()
    logger.info("stored %s (%d bytes) in workspace %s", rel_path, len(content), workspace.id)
    return record


def resolve_workspace_path(workspace_id: str, rel_path: str) -> Path:
    """Resolve a workspace-relative path, refusing anything that escapes the root."""
    settings = get_settings()
    root = settings.workspace_dir(workspace_id).resolve()
    candidate = (root / rel_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise IngestionError(f"path escapes the workspace: {rel_path}")
    return candidate


def delete_file(
    session: Session,
    workspace_id: str,
    record: DocumentFile,
    settings: Settings | None = None,
) -> None:
    """Remove a document from disk, from the vector index, and from the database."""
    settings = settings or get_settings()
    path = resolve_workspace_path(workspace_id, record.rel_path)
    try:
        VectorStore(settings).delete_file(workspace_id, record.id)
    except Exception as exc:  # the index is derived data; never block deletion on it
        logger.warning("could not remove %s from the vector index: %s", record.id, exc)
    path.unlink(missing_ok=True)
    session.delete(record)
    logger.info("deleted %s from workspace %s", record.rel_path, workspace_id)


def index_file(
    session: Session,
    workspace_id: str,
    record: DocumentFile,
    settings: Settings | None = None,
    *,
    embeddings=None,
    vector_store: VectorStore | None = None,
) -> IndexingResult:
    """Parse a document into parent/child chunks, persist them, and index the children.

    Only children are embedded and keyword-indexed, because only children are ranked.
    Parents exist to give the model context around a retrieved child; embedding them
    would double the cost of every re-index and add nothing to ranking quality.

    The chunk rows are the source of truth for keyword retrieval, so they are always
    written. The dense index is best-effort: when the embedding provider is
    unreachable the file still becomes searchable through BM25 rather than ending up
    entirely unindexed.

    SQLite allows exactly one writer, so parsing and embedding deliberately happen
    BEFORE the first write of this function: the write lock is then held only for
    the row swap (milliseconds), never for the seconds a local embedding model
    needs. Holding it across embedding starved every concurrent approval click
    until its transaction timed out with ``database is locked``.
    """
    settings = settings or get_settings()
    path = resolve_workspace_path(workspace_id, record.rel_path)
    if not path.exists():
        record.status = IndexStatus.failed
        record.error = "file is missing from disk"
        session.flush()
        return IndexingResult(
            record.id, record.rel_path, 0, 0, IndexStatus.failed, record.error
        )

    try:
        groups = chunk_document_groups(
            path,
            record.rel_path,
            chunk_size_tokens=settings.chunk_size_tokens,
            chunk_overlap_tokens=settings.chunk_overlap_tokens,
        )
    except Exception as exc:
        record.status = IndexStatus.failed
        record.error = f"parse failed: {exc}"
        session.flush()
        logger.exception("parsing failed for %s", record.rel_path)
        return IndexingResult(
            record.id, record.rel_path, 0, 0, IndexStatus.failed, record.error
        )

    # ---- preparation: build rows and embed BEFORE any DB write, so the SQLite
    # write lock is held only for the row swap, never for the model's runtime ----
    pending_parents: list[tuple[Chunk, list[Chunk]]] = []
    child_rows: list[Chunk] = []
    for group in groups:
        parent_counts = token_counts(group.parent.text)
        parent_row = Chunk(
            level="parent",
            ordinal=0,
            text=group.parent.text,
            location=group.parent.location,
            meta=group.parent.meta,
            token_counts=parent_counts,
            token_length=sum(parent_counts.values()),
        )
        children: list[Chunk] = []
        for child in group.children:
            child_counts = token_counts(child.text)
            child_row = Chunk(
                level="child",
                ordinal=0,
                text=child.text,
                location=child.location,
                meta=child.meta,
                token_counts=child_counts,
                token_length=sum(child_counts.values()),
            )
            children.append(child_row)
            child_rows.append(child_row)
        pending_parents.append((parent_row, children))

    vectors: list | None = None
    vector_error: str | None = None
    if child_rows:
        try:
            if embeddings is None:
                from ..llm.providers import build_embeddings

                embeddings = build_embeddings(settings)
            vectors = embeddings.embed_documents([row.text for row in child_rows])
            if len(vectors) != len(child_rows):
                raise VectorStoreError(
                    f"embedding provider returned {len(vectors)} vectors "
                    f"for {len(child_rows)} chunks"
                )
        except Exception as exc:
            vectors = None
            vector_error = str(exc)
            logger.warning(
                "dense indexing failed for %s, keyword search still available: %s",
                record.rel_path,
                exc,
            )

    # ---- write burst: row swap and final status only, no model calls inside ----
    record.status = IndexStatus.indexing
    record.error = None
    session.flush()

    # Re-indexing replaces rather than appends, so a file never contributes twice.
    session.execute(delete(Chunk).where(Chunk.file_id == record.id))
    session.flush()

    ordinal = 0
    for parent_row, _ in pending_parents:
        parent_row.workspace_id = workspace_id
        parent_row.file_id = record.id
        parent_row.ordinal = ordinal
        ordinal += 1
        session.add(parent_row)
    session.flush()  # ids exist before children reference them

    for parent_row, children in pending_parents:
        for child_row in children:
            child_row.workspace_id = workspace_id
            child_row.file_id = record.id
            child_row.parent_id = parent_row.id
            child_row.ordinal = ordinal
            ordinal += 1
            session.add(child_row)
    session.flush()

    vector_count = 0
    if vectors is not None:
        try:
            store = vector_store or VectorStore(settings)
            vector_count = store.upsert_vectors(
                workspace_id,
                [row.id for row in child_rows],
                vectors,
                [
                    {
                        "file_id": record.id,
                        "rel_path": record.rel_path,
                        "location": row.location,
                        "ordinal": row.ordinal,
                    }
                    for row in child_rows
                ],
            )
        except Exception as exc:
            vector_error = vector_error or str(exc)
            logger.warning(
                "dense index write failed for %s, keyword search still available: %s",
                record.rel_path,
                exc,
            )

    record.chunk_count = len(child_rows)
    record.checksum = _digest(path)
    record.indexed_at = datetime.now(timezone.utc)
    record.status = IndexStatus.indexed
    if vector_error:
        record.error = f"dense index unavailable: {vector_error}"
    session.flush()

    logger.info(
        "indexed %s: %d chunks, %d vectors", record.rel_path, len(child_rows), vector_count
    )
    return IndexingResult(
        file_id=record.id,
        rel_path=record.rel_path,
        chunk_count=len(child_rows),
        vector_count=vector_count,
        status=record.status,
        error=record.error,
    )


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def files_needing_index(session: Session, workspace_id: str) -> list[DocumentFile]:
    return list(
        session.scalars(
            select(DocumentFile).where(
                DocumentFile.workspace_id == workspace_id,
                DocumentFile.status != IndexStatus.indexed,
            )
        )
    )


def legacy_chunk_workspaces(session: Session) -> list[str]:
    """Workspaces still holding flat, pre-hierarchy chunks.

    Those rows cannot produce an accurate citation and are not filtered by the
    relevance gate the way children are, so they need a rebuild. Detection only
    reports the situation — rebuilding is an explicit operator action, because doing
    it at startup would block boot on a large corpus.
    """
    rows = session.execute(
        select(Chunk.workspace_id, func.count(Chunk.id))
        .where(Chunk.level != "parent")
        .group_by(Chunk.workspace_id)
    ).all()
    parents = set(
        session.scalars(select(Chunk.workspace_id).where(Chunk.level == "parent").distinct())
    )
    return [workspace_id for workspace_id, _ in rows if workspace_id not in parents]
