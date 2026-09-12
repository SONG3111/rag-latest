"""Qdrant-backed dense index, one collection per workspace.

Qdrant runs in local (embedded) mode so the application needs no external service.
Each workspace gets its own collection, which makes deletion a single call and keeps
one workspace's vectors from ever appearing in another's results.
"""

from __future__ import annotations

import logging
import uuid
from functools import lru_cache

from langchain_core.embeddings import Embeddings
from qdrant_client import QdrantClient
from qdrant_client.http import models as qmodels

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

# Qdrant rejects arbitrarily large upserts; embedding batches are far smaller anyway.
UPSERT_BATCH = 64


class VectorStoreError(RuntimeError):
    """Raised when the vector index cannot be reached or written."""


@lru_cache(maxsize=1)
def get_client() -> QdrantClient:
    settings = get_settings()
    settings.qdrant_dir.mkdir(parents=True, exist_ok=True)
    return QdrantClient(path=str(settings.qdrant_dir))


def close_client() -> None:
    """Release the embedded Qdrant handle.

    Each workspace keeps its own dense index, and all of them share a single client.
    Closing it explicitly at shutdown avoids the interpreter-shutdown ``__del__`` path,
    which otherwise prints a confusing ImportError traceback on exit.
    """
    if get_client.cache_info().currsize == 0:
        return
    try:
        get_client().close()
    except Exception:  # pragma: no cover - best-effort cleanup
        pass
    finally:
        get_client.cache_clear()


def collection_name(workspace_id: str) -> str:
    """Collection names must be filesystem-safe, so the workspace id is sanitized."""
    safe = "".join(char for char in workspace_id if char.isalnum() or char in "-_")
    return f"ws_{safe}"


def _point_id(chunk_id: str) -> str:
    """Qdrant accepts UUIDs or unsigned ints; our chunk ids are hex strings."""
    return str(uuid.UUID(chunk_id))


class VectorStore:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.client = get_client()

    def has_collection(self, workspace_id: str) -> bool:
        return self.client.collection_exists(collection_name(workspace_id))

    def ensure_collection(self, workspace_id: str) -> None:
        name = collection_name(workspace_id)
        if self.client.collection_exists(name):
            return
        self.client.create_collection(
            collection_name=name,
            vectors_config=qmodels.VectorParams(
                size=self.settings.embedding_dimensions,
                distance=qmodels.Distance.COSINE,
            ),
        )
        # Payload indexes are intentionally not created: in embedded (local) mode
        # Qdrant ignores them and emits a warning, and each workspace collection holds
        # only a few thousand points where a linear scan over the file_id filter is
        # effectively free. Revisit if this ever moves to a server deployment.

    def drop_collection(self, workspace_id: str) -> None:
        name = collection_name(workspace_id)
        if self.client.collection_exists(name):
            self.client.delete_collection(name)

    def upsert(
        self,
        workspace_id: str,
        embeddings: Embeddings,
        chunk_ids: list[str],
        texts: list[str],
        payloads: list[dict],
    ) -> int:
        """Embed and store chunks. Returns the number of points written."""
        if not chunk_ids:
            return 0
        vectors = embeddings.embed_documents(texts)
        if len(vectors) != len(chunk_ids):
            raise VectorStoreError(
                f"embedding provider returned {len(vectors)} vectors for {len(chunk_ids)} chunks"
            )
        return self.upsert_vectors(workspace_id, chunk_ids, vectors, payloads)

    def upsert_vectors(
        self,
        workspace_id: str,
        chunk_ids: list[str],
        vectors: list,
        payloads: list[dict],
    ) -> int:
        """Store already-embedded chunks. Returns the number of points written.

        Separate from ``upsert`` so callers that must not hold a database write
        lock can embed first and only then take the lock to swap their rows.
        """
        if not chunk_ids:
            return 0
        self.ensure_collection(workspace_id)

        name = collection_name(workspace_id)
        written = 0
        for start in range(0, len(chunk_ids), UPSERT_BATCH):
            window = slice(start, start + UPSERT_BATCH)
            batch_ids = chunk_ids[window]
            points = [
                qmodels.PointStruct(
                    id=_point_id(chunk_id),
                    vector=vector,
                    payload={**payload, "chunk_id": chunk_id},
                )
                for chunk_id, vector, payload in zip(
                    batch_ids, vectors[window], payloads[window]
                )
            ]
            self.client.upsert(collection_name=name, points=points, wait=True)
            written += len(points)
        return written

    def delete_file(self, workspace_id: str, file_id: str) -> None:
        name = collection_name(workspace_id)
        if not self.client.collection_exists(name):
            return
        self.client.delete(
            collection_name=name,
            points_selector=qmodels.FilterSelector(
                filter=qmodels.Filter(
                    must=[
                        qmodels.FieldCondition(
                            key="file_id", match=qmodels.MatchValue(value=file_id)
                        )
                    ]
                )
            ),
            wait=True,
        )

    def search(
        self,
        workspace_id: str,
        embeddings: Embeddings,
        query: str,
        top_k: int,
    ) -> list[str]:
        """Return chunk ids ordered by descending cosine similarity."""
        name = collection_name(workspace_id)
        if not self.client.collection_exists(name):
            return []
        vector = embeddings.embed_query(query)
        try:
            hits = self.client.query_points(
                collection_name=name,
                query=vector,
                limit=top_k,
                with_payload=True,
            ).points
        except Exception as exc:
            logger.warning("vector search failed: %s", exc)
            return []
        return [
            str(point.payload.get("chunk_id"))
            for point in hits
            if point.payload and point.payload.get("chunk_id")
        ]
