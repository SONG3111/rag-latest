"""Hybrid retrieval: rewrite, dense + sparse recall, dedup, fusion, rerank, gate.

Each stage exists for a reason worth stating explicitly, because this is the part of
the project most likely to be questioned:

1. **Rewrite.** "报销咋整" and "那第三条呢" are real user phrasings that neither
   leg can match well. Normalizing the query first lifts both.
2. **Dense recall** (Qdrant) handles paraphrase — the user asks "报销上限是多少"
   and the document says "单笔报销金额不得超过 5000 元".
3. **Sparse recall** (BM25) handles exact tokens that embeddings fuzz away — policy
   numbers, clause ids, names. Both legs returning the same passage is expected, so
   results are collapsed by content fingerprint before fusion to avoid one passage
   occupying two slots.
4. **RRF** merges the legs because BM25 scores and cosine similarities are not on a
   comparable scale; only their orderings are.
5. **Reranking** reads query and passage together with a cross-encoder, which fixes
   ordering that neither recall stage can.
6. **Relevance gate.** A cross-encoder score is an absolute signal, so passages below
   the floor are dropped. Without this stage the system always returns top-k, which
   means a question the corpus cannot answer still yields confident-looking context
   and the model invents an answer.

Retrieval operates on *child* chunks, which are precise (one row, one paragraph), and
attaches the enclosing *parent* as context, so a hit can be both exactly located and
fully interpretable.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from .bm25 import BM25Index, content_fingerprint, reciprocal_rank_fusion
from .rewriter import QueryRewriter
from .vector_store import VectorStore

logger = logging.getLogger(__name__)

SCORE_SOURCE_RERANK = "rerank"
SCORE_SOURCE_FUSED = "fused"


def _open_span(trace, node: str, input_summary: dict[str, Any] | None):
    """Open a trace span, or a no-op stand-in when tracing is off.

    The no-op record is a plain dict, so the pipeline can write ``span["output"]``
    unconditionally without branching on whether tracing is enabled.
    """
    if trace is None:
        return contextlib.nullcontext({"output": {}, "error": None})
    return trace.span(node, input_summary)


@dataclass
class ChunkRecord:
    """A chunk row as delivered over the internal retrieval-corpus endpoint.

    backend-java owns chunk persistence; the corpus always arrives through the
    injected loader (one internal HTTP call per search). Field names mirror the
    database columns, and the retrieval pipeline only ever touches these
    attributes.
    """

    id: str
    file_id: str
    parent_id: str | None
    level: str
    text: str
    location: str
    meta: dict[str, Any] = field(default_factory=dict)
    token_counts: dict[str, int] = field(default_factory=dict)
    token_length: int = 0
    ordinal: int = 0


#: (files: {file_id: rel_path}, chunks) — the shape a corpus loader returns.
Corpus = tuple[dict[str, str], list[ChunkRecord]]


@dataclass
class RetrievedChunk:
    """A retrieval hit with everything needed to cite it and to interpret it."""

    chunk_id: str
    text: str
    rel_path: str
    location: str
    score: float
    parent_text: str | None = None
    parent_location: str | None = None
    score_source: str = SCORE_SOURCE_FUSED
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def context_text(self) -> str:
        """Text handed to the model: the parent block when available, else the hit."""
        return self.parent_text or self.text

    def to_citation(self) -> dict[str, Any]:
        return {
            "chunk_id": self.chunk_id,
            "file": self.rel_path,
            "location": self.location,
            "snippet": (self.text or "")[:400],
            "score": self.score,
            "score_source": self.score_source,
            "parent_location": self.parent_location,
        }

    def to_reference(self) -> dict[str, Any]:
        """Shape returned to the agent through the knowledge-base tool."""
        return {
            "file": self.rel_path,
            "location": self.location,
            "text": self.context_text,
            "score": self.score,
            "score_source": self.score_source,
        }


@dataclass
class RetrievalRun:
    """Bookkeeping for the most recent search, used by evaluation and debugging."""

    original_query: str = ""
    effective_query: str = ""
    rewritten: bool = False
    rewrite_error: str | None = None
    threshold_applied: float | None = None
    filtered_by_threshold: int = 0
    candidates_before_dedup: int = 0
    candidates_after_dedup: int = 0


class Retriever:
    def __init__(
        self,
        workspace_id: str,
        settings: Settings | None = None,
        *,
        embeddings=None,
        reranker=None,
        vector_store: VectorStore | None = None,
        rewriter: QueryRewriter | None = None,
        corpus_loader: Callable[[], Corpus],
    ) -> None:
        self.workspace_id = workspace_id
        self.settings = settings or get_settings()
        self._embeddings = embeddings
        self._reranker = reranker
        self._vector_store = vector_store
        self._rewriter = rewriter
        # The corpus comes from one internal HTTP call per search (served by
        # Java's retrieval-corpus endpoint). The result is memoized for the
        # retriever's lifetime so children, parents, and the file map share it.
        self._corpus_loader = corpus_loader
        self._corpus: Corpus | None = None
        self._warned: set[str] = set()
        self.last_run = RetrievalRun()

    # ------------------------------------------------------------------ #
    # lazily built dependencies, so keyword-only retrieval still works
    # without an API key
    # ------------------------------------------------------------------ #
    @property
    def embeddings(self):
        if self._embeddings is None:
            from ..llm.providers import build_embeddings

            self._embeddings = build_embeddings(self.settings)
        return self._embeddings

    @property
    def reranker(self):
        if self._reranker is None:
            from ..llm.providers import build_reranker

            self._reranker = build_reranker(self.settings)
        return self._reranker

    @property
    def vector_store(self) -> VectorStore:
        if self._vector_store is None:
            self._vector_store = VectorStore(self.settings)
        return self._vector_store

    @property
    def rewriter(self) -> QueryRewriter:
        if self._rewriter is None:
            self._rewriter = QueryRewriter(self.settings)
        return self._rewriter

    def _warn_once(self, key: str, message: str) -> None:
        if key not in self._warned:
            self._warned.add(key)
            logger.warning(message)

    # ------------------------------------------------------------------ #
    # search
    # ------------------------------------------------------------------ #
    def _load_corpus(self) -> Corpus:
        """Fetch (and memoize) the corpus via the injected loader."""
        if self._corpus is None:
            files, chunks = self._corpus_loader()
            self._corpus = (files, list(chunks))
        return self._corpus

    def _load_children(self) -> list[ChunkRecord]:
        """Retrievable units only. Parents are context carriers, never ranked."""
        _, chunks = self._load_corpus()
        return [chunk for chunk in chunks if chunk.level == "child"]

    def _load_parents(self, parent_ids: set[str]) -> dict[str, ChunkRecord]:
        if not parent_ids:
            return {}
        _, chunks = self._load_corpus()
        return {chunk.id: chunk for chunk in chunks if chunk.id in parent_ids}

    @staticmethod
    def _dedupe(ranked_ids: list[str], by_id: dict[str, ChunkRecord]) -> list[str]:
        """Collapse passages that appear more than once, keeping the best position.

        A passage surfaced by both legs is normal; letting it consume two of the
        twenty candidate slots is not. Order is preserved and the first occurrence
        wins, so deduping never reshuffles the ranking.
        """
        seen: set[str] = set()
        deduped: list[str] = []
        for chunk_id in ranked_ids:
            chunk = by_id.get(chunk_id)
            if chunk is None:
                continue
            fingerprint = content_fingerprint(chunk.text)
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            deduped.append(chunk_id)
        return deduped

    def search(
        self,
        query: str,
        *,
        top_k: int | None = None,
        use_dense: bool = True,
        use_rerank: bool = True,
        use_rewrite: bool | None = None,
        history: list[str] | None = None,
        relevance_threshold: float | None = None,
        trace: "TraceCollector | None" = None,
    ) -> list[RetrievedChunk]:
        """Run the full pipeline and return the best passages.

        ``trace`` optionally receives one span per stage (rewrite / dense / bm25 /
        fuse / rerank / select), so a wrong answer can be attributed to the stage
        that produced it.
        """
        original = (query or "").strip()
        if not original:
            return []

        rewrite_enabled = (
            self.settings.query_rewrite_enabled if use_rewrite is None else use_rewrite
        )
        effective = original
        rewritten = False
        rewrite_error: str | None = None
        if rewrite_enabled:
            with _open_span(trace, "rewrite", {"query": original}) as span:
                result = self.rewriter.rewrite(original, history)
                effective = result.query
                rewritten = result.rewritten
                rewrite_error = result.error
                span["output"] = {
                    "effective_query": effective,
                    "rewritten": rewritten,
                    "error": rewrite_error,
                }

        run = RetrievalRun(
            original_query=original,
            effective_query=effective,
            rewritten=rewritten,
            rewrite_error=rewrite_error,
        )

        children = self._load_children()
        if not children:
            self.last_run = run
            return []
        by_id = {chunk.id: chunk for chunk in children}

        top_k = top_k or self.settings.rerank_top_n

        # --- sparse leg ---
        with _open_span(trace, "bm25", {"query": effective}) as span:
            bm25 = BM25Index(children)
            sparse_hits = bm25.search(effective, top_k=self.settings.retrieval_bm25_top_k)
            sparse_ids = [chunk_id for chunk_id, _ in sparse_hits]
            span["output"] = {"hits": len(sparse_ids)}

        # --- dense leg ---
        dense_ids: list[str] = []
        if use_dense:
            with _open_span(trace, "dense", {"query": effective}) as span:
                try:
                    dense_ids = self.vector_store.search(
                        self.workspace_id,
                        self.embeddings,
                        effective,
                        self.settings.retrieval_vector_top_k,
                    )
                    span["output"] = {"hits": len(dense_ids)}
                except Exception as exc:
                    self._warn_once(
                        "dense",
                        f"dense retrieval unavailable, continuing with BM25 only: {exc}",
                    )
                    span["error"] = str(exc)[:500]

        raw_lists = [ids for ids in (dense_ids, sparse_ids) if ids]
        if not raw_lists:
            self.last_run = run
            return []

        run.candidates_before_dedup = sum(len(ids) for ids in raw_lists)
        with _open_span(trace, "fuse", {"lists": [len(ids) for ids in raw_lists]}) as span:
            ranked_lists = [self._dedupe(ids, by_id) for ids in raw_lists]
            ranked_lists = [ids for ids in ranked_lists if ids]
            if not ranked_lists:
                self.last_run = run
                return []
            run.candidates_after_dedup = sum(len(ids) for ids in ranked_lists)
            span["output"] = {"candidates_after_dedup": run.candidates_after_dedup}

        if len(ranked_lists) == 1:
            fused = [
                (chunk_id, 1.0 / (1 + rank))
                for rank, chunk_id in enumerate(ranked_lists[0])
            ]
        else:
            # Dense recall is weighted slightly higher: BM25 over-triggers on chunks
            # that merely repeat a common term.
            fused = reciprocal_rank_fusion(ranked_lists, weights=[1.0, 0.8])

        candidates = [
            (chunk_id, score) for chunk_id, score in fused if chunk_id in by_id
        ][: self.settings.rerank_candidates]
        if not candidates:
            self.last_run = run
            return []

        candidate_ids = [chunk_id for chunk_id, _ in candidates]
        fused_scores = [score for _, score in candidates]
        candidate_texts = [by_id[chunk_id].text for chunk_id in candidate_ids]

        # --- rerank ---
        with _open_span(trace, "rerank", {"query": effective, "candidates": len(candidates)}) as span:
            order = list(range(len(candidates)))
            scores = list(fused_scores)
            score_source = SCORE_SOURCE_FUSED
            if use_rerank and len(candidates) > 1:
                try:
                    order, scores = self.reranker.rerank(
                        effective, candidate_texts, top_k, fused_scores
                    )
                    if getattr(self.reranker, "produces_relevance_scores", False):
                        score_source = SCORE_SOURCE_RERANK
                except Exception as exc:
                    self._warn_once("rerank", f"reranking failed, keeping fused order: {exc}")
                    order, scores = list(range(len(candidates))), list(fused_scores)
            span["output"] = {"score_source": score_source}

        # --- relevance gate ---
        # Only meaningful when the scores are actual relevance estimates; applying a
        # threshold to fused ranks would filter arbitrarily.
        threshold = (
            self.settings.rerank_score_threshold
            if relevance_threshold is None
            else relevance_threshold
        )
        gate_active = (
            use_rerank
            and score_source == SCORE_SOURCE_RERANK
            and threshold is not None
            and threshold > 0
        )
        run.threshold_applied = threshold if gate_active else None

        files, _ = self._load_corpus()

        selected: list[tuple[str, float]] = []
        for position, chunk_id in enumerate(order[: max(top_k, len(order))]):
            if position >= len(candidate_ids):
                continue
            score = scores[position] if position < len(scores) else fused_scores[position]
            if gate_active and score < threshold:
                run.filtered_by_threshold += 1
                continue
            selected.append((candidate_ids[position], float(score)))
            if len(selected) >= top_k:
                break

        parent_lookup = self._load_parents(
            {
                by_id[chunk_id].parent_id
                for chunk_id, _ in selected
                if by_id[chunk_id].parent_id
            }
        )

        results: list[RetrievedChunk] = []
        for chunk_id, score in selected:
            chunk = by_id[chunk_id]
            parent = parent_lookup.get(chunk.parent_id) if chunk.parent_id else None
            results.append(
                RetrievedChunk(
                    chunk_id=chunk.id,
                    text=chunk.text,
                    rel_path=files.get(chunk.file_id, "未知文件"),
                    location=chunk.location,
                    score=round(score, 6),
                    parent_text=parent.text if parent else None,
                    parent_location=parent.location if parent else None,
                    score_source=score_source,
                    meta=chunk.meta or {},
                )
            )

        self.last_run = run
        if trace is not None:
            with _open_span(trace, "select", {"threshold_applied": run.threshold_applied}) as span:
                span["output"] = {
                    "returned": len(results),
                    "filtered_by_threshold": run.filtered_by_threshold,
                    "files": sorted({result.rel_path for result in results}),
                }
        return results


def format_context(chunks: list[RetrievedChunk]) -> str:
    """Render retrieved passages into an attributable context block for the prompt."""
    if not chunks:
        return "（知识库中没有检索到相关内容）"
    blocks = []
    for index, chunk in enumerate(chunks, start=1):
        blocks.append(
            f"[资料{index}] 文件：{chunk.rel_path} · 位置：{chunk.location}\n"
            f"{chunk.context_text}"
        )
    return "\n\n".join(blocks)
