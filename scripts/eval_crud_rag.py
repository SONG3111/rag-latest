"""CRUD-RAG retrieval & chunking evaluation over the authoritative Chinese benchmark.

Previous rounds measured chunking on CMRC 2018 + a simulated business corpus with
BM25 only (``eval_chunking.py``). This script grades the *whole hybrid pipeline*
(bm25 / dense / RRF fusion / rerank) on **CRUD-RAG** (IAAR-Shanghai, arXiv:2401.17043)
— the community-standard Chinese RAG benchmark whose Read task is single-hop QA over
a news corpus. The prepared subset lives in ``tests/datasets/crud_rag/``: 3,000 news
documents (median 707 chars) and 86 labeled questions, each bound to the one document
that answers it, with a reference answer kept alongside for human inspection.

Protocol (mirrors ``pipeline.py`` stage by stage on in-memory records, so a full
sweep needs no 3,000-file ingestion):

1. Every txt is wrapped as a one-paragraph docx — the format production actually
   parses — and chunked by the real ``chunk_word_groups`` per (size, overlap) config.
2. Children are embedded with the production embedding model (local bge-m3, the
   same weights ``/v1/index`` uses), cached to ``data/rag-eval/crud-cache/`` keyed
   by config so re-runs only pay for new configs.
3. Modes, all fingerprint-deduped like the pipeline:
   ``bm25``  — sparse leg only.
   ``dense`` — cosine over the child matrix.
   ``hybrid`` — RRF fusion, dense weighted 1.0 vs bm25 0.8 (production weights).
   ``rerank`` — hybrid candidates → cross-encoder → top-k, i.e. the full pipeline.
4. Grading is file-level HitRate@k / MRR@k: the labeled document must surface in
   the top-k. CRUD-RAG's own labels are document-granular (their chunker yields
   ~1.26 chunks/doc, and the labeled chunk ``qa_XXXX_news1.txt#0`` is effectively
   the whole article), so file-level is the honest grain — answer-string matching
   would grade paraphrase, not retrieval.
5. Two cheap side-sweeps at the production config: RRF weight variants (no model
   calls) and rerank-candidate counts (batched cloud rerank).
6. An equivalence check runs the *real* ``Retriever`` (corpus_loader + Qdrant) on a
   12-file sub-workspace and compares hit lists with the fast path.

The rerank leg defaults to the DashScope text-rerank API (batched, scores are
per-pair so batching cannot change the ordering) because the local cross-encoder
runs at ~3 pairs/s on CPU; ``--rerank-mode local`` switches to bge-reranker-v2-m3.

Usage:
    python scripts/eval_crud_rag.py                          # full sweep, cloud rerank
    python scripts/eval_crud_rag.py --sizes 512 --no-rerank  # retrieval legs only
    python scripts/eval_crud_rag.py --out docs/rag-test-report/data/crud-rag-eval.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path

# Multiple OpenMP runtimes ship with torch/transformers on Windows; without this
# the first model import aborts the process (see providers.py proxy note — same
# class of environment-vs-library friction).
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai-service"))
sys.path.insert(0, str(ROOT / "scripts"))

DATASET_DIR = ROOT / "tests" / "datasets" / "crud_rag"
CACHE_DIR = ROOT / "data" / "rag-eval" / "crud-cache"
DOCX_DIR = CACHE_DIR / "docx"
EMBED_BATCH = 64
TOP_K = 10
RRF_WEIGHT_SWEEP = [
    ("prod-1.0-0.8", 1.0, 0.8),
    ("equal-1.0-1.0", 1.0, 1.0),
    ("dense-heavy-1.2-0.8", 1.2, 0.8),
    ("sparse-heavy-0.8-1.0", 0.8, 1.0),
]


@dataclass
class Child:
    """The retrievable unit, mirroring the persisted Chunk fields BM25 needs."""

    id: str
    text: str
    rel_path: str
    location: str
    parent_index: int
    token_counts: dict
    token_length: int


@dataclass
class Parent:
    text: str
    location: str
    rel_path: str


@dataclass
class Query:
    query_id: str
    query: str
    answer: str
    label_file: str  # docx name in the wrapped corpus


# --------------------------------------------------------------------------- #
# corpus preparation
# --------------------------------------------------------------------------- #
def load_evidence() -> list[Query]:
    queries = []
    for line in (DATASET_DIR / "evidence.jsonl").read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        # One labeled chunk per query in this subset; strip the chunk ordinal and
        # re-suffix to the docx name used in the wrapped corpus.
        source_name = row["relevant_chunks"][0].split("#")[0]
        queries.append(
            Query(
                query_id=row["query_id"],
                query=row["query"],
                answer=row.get("answer", ""),
                label_file=source_name[:-4] + ".docx" if source_name.endswith(".txt") else source_name,
            )
        )
    return queries


def wrap_corpus() -> list[Path]:
    """Convert every corpus txt into a one-paragraph docx, once, idempotently."""
    from docx import Document

    sources = sorted((DATASET_DIR / "docs").glob("*.txt"))
    if not sources:
        raise SystemExit(f"no corpus documents under {DATASET_DIR / 'docs'}")
    DOCX_DIR.mkdir(parents=True, exist_ok=True)
    targets = []
    for source in sources:
        target = DOCX_DIR / (source.stem + ".docx")
        if not target.exists():
            document = Document()
            document.add_paragraph(source.read_text(encoding="utf-8"))
            document.save(str(target))
        targets.append(target)
    return targets


def build_index(
    docx_files: list[Path],
    *,
    size: int,
    overlap: int,
) -> tuple[list[Child], list[Parent]]:
    """Chunk with the production factory, so the sweep measures what ships.

    ``build_body_splitter`` resolves the embedding tokenizer from settings and is
    the exact code path ``index_file`` takes (chunk_document_groups →
    build_body_splitter), keeping the sweep honest about the splitter under test.
    """
    from app.retrieval.bm25 import token_counts
    from app.retrieval.chunking import build_body_splitter, chunk_word_groups

    body_splitter = build_body_splitter(size, overlap)

    children: list[Child] = []
    parents: list[Parent] = []
    for path in docx_files:
        rel_path = path.name
        for group in chunk_word_groups(
            path,
            rel_path,
            chunk_size_tokens=size,
            chunk_overlap_tokens=overlap,
            body_splitter=body_splitter,
        ):
            parent_index = len(parents)
            parents.append(Parent(group.parent.text, group.parent.location, rel_path))
            for ordinal, child in enumerate(group.children):
                counts = token_counts(child.text)
                children.append(
                    Child(
                        id=f"{rel_path}#{parent_index}.{ordinal}",
                        text=child.text,
                        rel_path=rel_path,
                        location=child.location,
                        parent_index=parent_index,
                        token_counts=counts,
                        token_length=sum(counts.values()),
                    )
                )
    return children, parents


# --------------------------------------------------------------------------- #
# embeddings (cached per config)
# --------------------------------------------------------------------------- #
def _corpus_fingerprint(texts: list[str]) -> str:
    digest = hashlib.sha256()
    for text in texts:
        digest.update(hashlib.sha256(text.encode("utf-8")).digest())
    return digest.hexdigest()[:16]


def embed_children(children: list[Child], *, size: int, overlap: int) -> "list[list[float]]":
    import numpy as np

    from app.config import get_settings
    from app.llm.providers import build_embeddings

    cache_key = f"children-{size}x{overlap}-{_corpus_fingerprint([c.text for c in children])}"
    matrix_path = CACHE_DIR / f"{cache_key}.npy"
    if matrix_path.exists():
        matrix = np.load(matrix_path)
        if len(matrix) == len(children):
            print(f"  embeddings cache hit: {matrix_path.name}")
            return matrix
    embeddings = build_embeddings(get_settings())
    vectors: list[list[float]] = []
    started = time.time()
    for start in range(0, len(children), EMBED_BATCH):
        batch = [c.text for c in children[start : start + EMBED_BATCH]]
        vectors.extend(embeddings.embed_documents(batch))
        done = min(start + EMBED_BATCH, len(children))
        rate = done / max(time.time() - started, 1e-9)
        print(
            f"  embedding {done}/{len(children)} ({rate:.1f}/s)",
            flush=True,
        )
    matrix = np.asarray(vectors, dtype=np.float32)
    np.save(matrix_path, matrix)
    return matrix


def embed_queries(queries: list[Query]) -> "list[list[float]]":
    from app.config import get_settings
    from app.llm.providers import build_embeddings

    cache_path = CACHE_DIR / "queries.npy"
    import numpy as np

    if cache_path.exists():
        matrix = np.load(cache_path)
        if len(matrix) == len(queries):
            return matrix
    embeddings = build_embeddings(get_settings())
    vectors = embeddings.embed_documents([q.query for q in queries])
    matrix = np.asarray(vectors, dtype=np.float32)
    np.save(cache_path, matrix)
    return matrix


# --------------------------------------------------------------------------- #
# retrieval modes (pipeline.py mirrored in memory)
# --------------------------------------------------------------------------- #
def _dedupe(ranked_ids: list[str], by_id: dict[str, Child]) -> list[str]:
    from app.retrieval.bm25 import content_fingerprint

    seen: set[str] = set()
    deduped = []
    for chunk_id in ranked_ids:
        child = by_id.get(chunk_id)
        if child is None:
            continue
        fingerprint = content_fingerprint(child.text)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        deduped.append(chunk_id)
    return deduped


def search_bm25(index, children, query: str, top_k: int) -> list[str]:
    return [chunk_id for chunk_id, _ in index.search(query, top_k=top_k)]


def search_dense(matrix, query_vec, children, top_k: int) -> list[str]:
    import numpy as np

    scores = matrix @ np.asarray(query_vec, dtype=np.float32)
    order = np.argsort(-scores)[:top_k]
    return [children[i].id for i in order]


def fuse(dense_ids: list[str], bm25_ids: list[str], weights: tuple[float, float]) -> list[str]:
    from app.retrieval.bm25 import reciprocal_rank_fusion

    fused = reciprocal_rank_fusion([dense_ids, bm25_ids], weights=list(weights))
    return [chunk_id for chunk_id, _ in fused]


def rerank_api(query: str, texts: list[str], top_n: int, batch_size: int = 10) -> tuple[list[int], list[float]]:
    """Cloud text-rerank in fixed-size batches.

    A cross-encoder scores each (query, document) pair independently, so batching
    cannot change any score — merged batches are exactly equivalent to one big
    request, they only respect the endpoint's per-request document budget.
    """
    from app.config import get_settings
    from app.llm.providers import DashScopeReranker

    reranker = DashScopeReranker(get_settings())
    indices: list[int] = []
    scores: list[float] = []
    for start in range(0, len(texts), batch_size):
        batch = texts[start : start + batch_size]
        order, batch_scores = reranker.rerank(query, batch, len(batch))
        indices.extend(start + i for i in order)
        scores.extend(batch_scores)
    ranked = sorted(zip(indices, scores), key=lambda pair: -pair[1])[:top_n]
    return [i for i, _ in ranked], [s for _, s in ranked]


def rerank_local(query: str, texts: list[str], top_n: int) -> tuple[list[int], list[float]]:
    from app.config import get_settings
    from app.llm.providers import build_reranker

    reranker = build_reranker(get_settings())
    return reranker.rerank(query, texts, top_n)


# --------------------------------------------------------------------------- #
# grading
# --------------------------------------------------------------------------- #
def grade_modes(
    queries: list[Query],
    children: list[Child],
    parents: list[Parent],
    *,
    bm25_index,
    matrix,
    query_vecs,
    weights: tuple[float, float],
    rerank: str | None,
    candidates: int,
) -> dict:
    """Run every query through each mode; return HitRate@k / MRR@k per mode.

    ``mode_rankings`` keeps the per-query ranked lists for the failure appendix.
    """
    by_id = {child.id: child for child in children}
    ks = (1, 3, 5, 10)
    rankings: dict[str, list[list[str]]] = {mode: [] for mode in _mode_names(rerank)}
    for position, query in enumerate(queries):
        bm25_ids = _dedupe(search_bm25(bm25_index, children, query.query, TOP_K * 2), by_id)
        dense_ids = _dedupe(search_dense(matrix, query_vecs[position], children, TOP_K * 2), by_id)
        hybrid_ids = fuse(dense_ids, bm25_ids, weights)
        rankings["bm25"].append(bm25_ids)
        rankings["dense"].append(dense_ids)
        rankings["hybrid"].append(hybrid_ids)
        if rerank:
            pool = hybrid_ids[:candidates]
            texts = [by_id[cid].text for cid in pool]
            if rerank == "cloud":
                order, _ = rerank_api(query.query, texts, len(pool))
            else:
                order, _ = rerank_local(query.query, texts, len(pool))
            rankings["rerank"].append([pool[i] for i in order])

    summary: dict[str, dict] = {}
    for mode, ranked_lists in rankings.items():
        hits = {k: 0 for k in ks}
        rr: list[float] = []
        failures: list[dict] = []
        for query, ranked in zip(queries, ranked_lists):
            rank = 0
            for position, chunk_id in enumerate(ranked, start=1):
                if by_id[chunk_id].rel_path == query.label_file:
                    rank = position
                    break
            for k in ks:
                hits[k] += int(0 < rank <= k)
            rr.append(1.0 / rank if rank else 0.0)
            if not rank:
                failures.append(
                    {
                        "query_id": query.query_id,
                        "query": query.query,
                        "label_file": query.label_file,
                        "top3_files": [by_id[cid].rel_path for cid in ranked[:3]],
                    }
                )
        total = len(queries)
        summary[mode] = {
            **{f"hit@{k}": round(hits[k] / total, 4) for k in ks},
            "mrr@10": round(sum(rr) / total, 4),
            "failures": failures,
        }
    return summary


def _mode_names(rerank: str | None) -> list[str]:
    modes = ["bm25", "dense", "hybrid"]
    if rerank:
        modes.append("rerank")
    return modes


# --------------------------------------------------------------------------- #
# equivalence check against the real pipeline
# --------------------------------------------------------------------------- #
def equivalence_check(
    docx_files: list[Path], queries: list[Query], *, size: int, overlap: int
) -> dict:
    """Run the real Retriever (corpus_loader + Qdrant) on a small workspace; compare.

    Compares the hybrid (no rerank) ordering: both paths must apply the same
    fingerprint dedupe and the same RRF weights over the same chunk texts.
    """
    import tempfile
    import uuid

    from app.config import Settings
    from app.llm.providers import build_embeddings
    from app.retrieval.bm25 import token_counts
    from app.retrieval.chunking import chunk_document_groups
    from app.retrieval.pipeline import ChunkRecord, Retriever
    from app.retrieval.vector_store import VectorStore

    sample_names = {q.label_file for q in queries[:12]}
    sample_files = sorted(p for p in docx_files if p.name in sample_names)

    with tempfile.TemporaryDirectory(prefix="crud-equiv-") as tmp:
        settings = Settings(
            data_dir=Path(tmp) / "data",
            chunk_size_tokens=size,
            chunk_overlap_tokens=overlap,
            query_rewrite_enabled=False,
        )
        settings.ensure_directories()

        # 与 /v1/index 相同的分块与稠密索引路径，但 chunk 行留在内存里
        # （持久化归 backend-java），检索语料经 corpus_loader 注入。
        workspace_id = "crud-equiv"
        records: list[ChunkRecord] = []
        files: dict[str, str] = {}
        child_ids: list[str] = []
        child_texts: list[str] = []
        payloads: list[dict] = []
        for path in sample_files:
            file_id = f"f-{len(files)}"
            files[file_id] = path.name
            for ordinal, group in enumerate(
                chunk_document_groups(
                    path,
                    path.name,
                    chunk_size_tokens=size,
                    chunk_overlap_tokens=overlap,
                )
            ):
                parent_id = uuid.uuid4().hex
                parent_counts = token_counts(group.parent.text)
                records.append(
                    ChunkRecord(
                        id=parent_id,
                        file_id=file_id,
                        parent_id=None,
                        level="parent",
                        text=group.parent.text,
                        location=group.parent.location,
                        meta=group.parent.meta,
                        token_counts=parent_counts,
                        token_length=sum(parent_counts.values()),
                    )
                )
                for child in group.children:
                    child_counts = token_counts(child.text)
                    child_id = uuid.uuid4().hex
                    records.append(
                        ChunkRecord(
                            id=child_id,
                            file_id=file_id,
                            parent_id=parent_id,
                            level="child",
                            text=child.text,
                            location=child.location,
                            meta=child.meta,
                            token_counts=child_counts,
                            token_length=sum(child_counts.values()),
                        )
                    )
                    child_ids.append(child_id)
                    child_texts.append(child.text)
                    payloads.append(
                        {"file_id": file_id, "rel_path": path.name}
                    )
        corpus = (files, records)

        store = VectorStore(settings)
        store.drop_collection(workspace_id)
        store.upsert_vectors(
            workspace_id,
            child_ids,
            build_embeddings(settings).embed_documents(child_texts),
            payloads,
        )

        retriever = Retriever(
            workspace_id, settings=settings, corpus_loader=lambda: corpus
        )
        children, parents = build_index(docx_files, size=size, overlap=overlap)
        children = [c for c in children if c.rel_path in sample_names]
        by_id = {child.id: child for child in children}

        from app.retrieval.bm25 import BM25Index

        bm25_index = BM25Index(children)
        matrix = embed_children(children, size=size, overlap=overlap)
        sub_queries = [q for q in queries if q.label_file in sample_names][:10]
        query_vecs = embed_queries(sub_queries)

        mismatches = []
        for position, query in enumerate(sub_queries):
            real = retriever.search(
                query.query, top_k=TOP_K, use_dense=True, use_rerank=False, use_rewrite=False
            )
            real_keys = [(r.rel_path, r.location) for r in real]
            bm25_ids = _dedupe(search_bm25(bm25_index, children, query.query, TOP_K * 2), by_id)
            dense_ids = _dedupe(search_dense(matrix, query_vecs[position], children, TOP_K * 2), by_id)
            fused = fuse(dense_ids, bm25_ids, (1.0, 0.8))[:TOP_K]
            fast_keys = [
                (children_map.rel_path, children_map.location)
                for children_map in (by_id[cid] for cid in fused)
            ]
            if real_keys != fast_keys:
                mismatches.append({"query": query.query, "real": real_keys[:3], "fast": fast_keys[:3]})
        store.drop_collection(workspace_id)
    return {"checked": len(sub_queries), "mismatches": len(mismatches), "detail": mismatches[:3]}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD-RAG retrieval & chunking evaluation")
    parser.add_argument("--sizes", default="128,256,512,1024")
    parser.add_argument("--overlap", type=int, default=64)
    parser.add_argument("--rerank-mode", choices=["cloud", "local", "none"], default="local",
                        help="local = bge-reranker-v2-m3 (production default); cloud = dashscope text-rerank")
    parser.add_argument("--rerank-configs", default="512x64",
                        help="comma list of SIZExOVERLAP configs that get the rerank leg")
    parser.add_argument("--candidates", type=int, default=20)
    parser.add_argument("--candidates-sweep", default="50",
                        help="extra rerank candidate counts at the production config")
    parser.add_argument("--limit-docs", type=int, default=0,
                        help="smoke-test only: first N corpus docs")
    parser.add_argument("--limit-queries", type=int, default=0,
                        help="smoke-test only: first N queries")
    parser.add_argument("--skip-equivalence", action="store_true")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    sizes = [int(v) for v in args.sizes.split(",")]
    rerank_mode = None if args.rerank_mode == "none" else args.rerank_mode
    rerank_configs = {
        tuple(int(x) for x in item.split("x")) for item in args.rerank_configs.split(",") if item
    }

    from app.config import get_settings

    production = get_settings()
    print(
        f"rerank leg: {rerank_mode or 'off'}; production config "
        f"{production.chunk_size_tokens}x{production.chunk_overlap_tokens}"
    )

    queries = load_evidence()
    docx_files = wrap_corpus()
    if args.limit_docs:
        labeled = {q.label_file for q in queries}
        keep = {p for p in docx_files if p.name in labeled}
        keep.update(docx_files[: args.limit_docs])
        docx_files = sorted(keep)
    if args.limit_queries:
        queries = queries[: args.limit_queries]
    print(f"corpus: {len(docx_files)} docs, queries: {len(queries)}")

    query_vecs = embed_queries(queries)

    sweep_results = []
    config_states: dict[tuple[int, int], dict] = {}
    for size in sizes:
        overlap = args.overlap
        config = (size, overlap)
        print(f"== config {size}x{overlap} ==", flush=True)
        started = time.time()
        children, parents = build_index(docx_files, size=size, overlap=overlap)
        build_seconds = round(time.time() - started, 1)
        matrix = embed_children(children, size=size, overlap=overlap)

        from app.retrieval.bm25 import BM25Index

        bm25_index = BM25Index(children)
        config_states[config] = {
            "children": children,
            "parents": parents,
            "bm25": bm25_index,
            "matrix": matrix,
        }

        do_rerank = rerank_mode if config in rerank_configs else None
        summary = grade_modes(
            queries, children, parents,
            bm25_index=bm25_index, matrix=matrix, query_vecs=query_vecs,
            weights=(1.0, 0.8), rerank=do_rerank, candidates=args.candidates,
        )
        sweep_results.append(
            {
                "size": size,
                "overlap": overlap,
                "children": len(children),
                "parents": len(parents),
                "build_seconds": build_seconds,
                "modes": summary,
            }
        )
        for mode, stats in summary.items():
            print(
                f"  {mode:<7} hit@5={stats['hit@5']:.4f} hit@10={stats['hit@10']:.4f} "
                f"mrr@10={stats['mrr@10']:.4f}"
            )

    # ---- RRF weight sweep at the production config (no model calls) ----
    prod_config = (production.chunk_size_tokens, production.chunk_overlap_tokens)
    rrf_results = []
    if prod_config in config_states:
        state = config_states[prod_config]
        for label, dense_w, bm25_w in RRF_WEIGHT_SWEEP:
            summary = grade_modes(
                queries, state["children"], state["parents"],
                bm25_index=state["bm25"], matrix=state["matrix"], query_vecs=query_vecs,
                weights=(dense_w, bm25_w), rerank=None, candidates=args.candidates,
            )
            rrf_results.append(
                {"label": label, "dense_weight": dense_w, "bm25_weight": bm25_w, "modes": summary}
            )
            print(
                f"rrf {label:<24} hybrid hit@5={summary['hybrid']['hit@5']:.4f} "
                f"mrr@10={summary['hybrid']['mrr@10']:.4f}"
            )

    # ---- rerank candidate sweep at the production config ----
    candidates_results = []
    if rerank_mode and prod_config in config_states:
        state = config_states[prod_config]
        for count in [int(v) for v in args.candidates_sweep.split(",") if v.strip()]:
            summary = grade_modes(
                queries, state["children"], state["parents"],
                bm25_index=state["bm25"], matrix=state["matrix"], query_vecs=query_vecs,
                weights=(1.0, 0.8), rerank=rerank_mode, candidates=count,
            )
            candidates_results.append({"candidates": count, "modes": summary})
            print(
                f"candidates={count:<3} rerank hit@5={summary['rerank']['hit@5']:.4f} "
                f"mrr@10={summary['rerank']['mrr@10']:.4f}"
            )

    equivalence = {"skipped": True}
    if not args.skip_equivalence:
        print("equivalence check (real Retriever on a 12-file workspace)…", flush=True)
        equivalence = equivalence_check(
            docx_files, queries, size=prod_config[0], overlap=prod_config[1]
        )
        print(f"equivalence: {equivalence['mismatches']}/{equivalence['checked']} mismatches")

    payload = {
        "dataset": {
            "name": "CRUD-RAG (Read / single-hop QA subset)",
            "source": "IAAR-Shanghai CRUD-RAG, arXiv:2401.17043",
            "docs": len(docx_files),
            "queries": len(queries),
            "preparation": "tests/datasets/crud_rag/prepare_stats.json",
        },
        "embedding": {
            "provider": production.embedding_provider,
            "model": "bge-m3 (local)" if production.embedding_provider == "local" else production.embedding_model,
        },
        "reranker": rerank_mode or "off",
        "top_k": TOP_K,
        "sweep": sweep_results,
        "rrf_weight_sweep": rrf_results,
        "candidates_sweep": candidates_results,
        "equivalence": equivalence,
    }
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
