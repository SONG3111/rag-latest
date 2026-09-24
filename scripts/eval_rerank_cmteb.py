"""Vector retrieval & reranking evaluation on C-MTEB T2Reranking (official protocol).

``eval_crud_rag.py`` grades the full pipeline on CRUD-RAG, where relevance is
document-granular and pools are built by retrieval. This script complements it
with **T2Ranking/T2Reranking** (arXiv:2304.03679), the task behind C-MTEB's
Reranking leaderboard, where every query ships with its *own candidate pool*
(positives + sampled negatives — `pools.jsonl`, rebuilt verbatim from the
official dev parquet). That matches how MTEB scores the task (``AbsTaskRetrieval``,
main score MAP, plus NDCG@10 / MRR@10) and also how a RAG reranker actually
operates: reorder a recall-stage pool, never the whole corpus.

Setup: 500 queries, 8,208 passages (pool union), pools min/p50/max = 1/15/78.
Modes — all ordering the *same official pool*, so differences are pure ranking:

* ``bm25``   — jieba-bigram BM25 (the production sparse leg) ranks the pool.
* ``dense``  — bge-m3 cosine (the production dense leg).
* ``rrf``    — production fusion (dense weight 1.0, bm25 0.8, k=60).
* ``rerank`` — bge-reranker-v2-m3 cross-encoder, the production reranker.

Also reported: candidate-generation recall (how much of each pool a top-30
retrieval leg actually captures) — the number that decides ``rerank_candidates``
in a retrieval-built pipeline.

Model calls are real but local (bge-m3 / bge-reranker-v2-m3 weights on disk);
embeddings are cached under ``data/rag-eval/cmteb-cache/``.

Usage:
    python scripts/eval_rerank_cmteb.py --out docs/rag-test-report/data/cmteb-rerank-eval.json
    python scripts/eval_rerank_cmteb.py --limit-queries 50   # smoke run
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai-service"))
sys.path.insert(0, str(ROOT / "scripts"))

DATASET_DIR = ROOT / "tests" / "datasets" / "cmteb_reranking"
CACHE_DIR = ROOT / "data" / "rag-eval" / "cmteb-cache"
EMBED_BATCH = 64


# --------------------------------------------------------------------------- #
# data
# --------------------------------------------------------------------------- #
def load_dataset(limit_queries: int = 0):
    corpus = [
        json.loads(line)
        for line in (DATASET_DIR / "corpus.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    evidence = [
        json.loads(line)
        for line in (DATASET_DIR / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    pools = {
        row["query_id"]: row["pool_pids"]
        for row in (
            json.loads(line)
            for line in (DATASET_DIR / "pools.jsonl").read_text(encoding="utf-8").splitlines()
        )
    }
    if limit_queries:
        evidence = evidence[:limit_queries]
    return corpus, evidence, pools


# --------------------------------------------------------------------------- #
# embeddings (cached)
# --------------------------------------------------------------------------- #
def _fingerprint(texts: list[str]) -> str:
    import hashlib

    digest = hashlib.sha256()
    for text in texts:
        digest.update(hashlib.sha256(text.encode("utf-8")).digest())
    return digest.hexdigest()[:16]


def _embed(texts: list[str], name: str) -> "list[list[float]]":
    import numpy as np

    from app.config import get_settings
    from app.llm.providers import build_embeddings

    # T2Ranking passages run long (p90 ≈ 1,780 chars ≈ 2,500+ XLM-R tokens); at
    # bge-m3's full 8,192-token window a CPU encode pass takes hours. 512 tokens
    # is bge-m3's own retrieval guidance, the production child budget, and the
    # production reranker's truncation point — aligning the dense leg with both.
    TRUNCATE_TOKENS = 512

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    matrix_path = CACHE_DIR / f"{name}-t{TRUNCATE_TOKENS}-{_fingerprint(texts)}.npy"
    if matrix_path.exists():
        matrix = np.load(matrix_path)
        if len(matrix) == len(texts):
            print(f"  embeddings cache hit: {name} ({len(texts)} texts)")
            return matrix
    embeddings = build_embeddings(get_settings())
    # langchain-huggingface renamed its handle across versions; try both, and
    # fail loudly rather than silently encoding at the 8k window for hours.
    st_model = getattr(embeddings, "client", None) or getattr(embeddings, "_client", None)
    if st_model is None or not hasattr(st_model, "max_seq_length"):
        raise RuntimeError("cannot set max_seq_length on the embedding model")
    st_model.max_seq_length = TRUNCATE_TOKENS
    vectors: list[list[float]] = []
    started = time.time()
    for start in range(0, len(texts), EMBED_BATCH):
        vectors.extend(embeddings.embed_documents(texts[start : start + EMBED_BATCH]))
        done = min(start + EMBED_BATCH, len(texts))
        rate = done / max(time.time() - started, 1e-9)
        print(f"  embedding {name} {done}/{len(texts)} ({rate:.1f}/s)", flush=True)
    matrix = np.asarray(vectors, dtype=np.float32)
    np.save(matrix_path, matrix)
    return matrix


# --------------------------------------------------------------------------- #
# metrics (official T2Reranking set: MAP, NDCG@10, MRR@10; plus Recall@10)
# --------------------------------------------------------------------------- #
def average_precision(ranked: list[str], relevant: set[str]) -> float:
    if not relevant:
        return 0.0
    hits = 0
    precision_sum = 0.0
    for position, pid in enumerate(ranked, start=1):
        if pid in relevant:
            hits += 1
            precision_sum += hits / position
    return precision_sum / len(relevant)


def ndcg_at_k(ranked: list[str], relevant: set[str], k: int = 10) -> float:
    if not relevant:
        return 0.0
    dcg = sum(
        1.0 / math.log2(position + 1)
        for position, pid in enumerate(ranked[:k], start=1)
        if pid in relevant
    )
    ideal = sum(1.0 / math.log2(position + 1) for position in range(1, min(len(relevant), k) + 1))
    return dcg / ideal


def mrr_at_k(ranked: list[str], relevant: set[str], k: int = 10) -> float:
    for position, pid in enumerate(ranked[:k], start=1):
        if pid in relevant:
            return 1.0 / position
    return 0.0


def recall_at_k(ranked: list[str], relevant: set[str], k: int = 10) -> float:
    if not relevant:
        return 0.0
    return sum(1 for pid in ranked[:k] if pid in relevant) / len(relevant)


def evaluate(rankings: dict[str, list[str]], relevant_by_query: dict[str, set[str]]) -> dict:
    queries = list(rankings)
    per_metric = {"map": [], "ndcg@10": [], "mrr@10": [], "recall@10": []}
    for query_id in queries:
        relevant = relevant_by_query[query_id]
        ranked = rankings[query_id]
        per_metric["map"].append(average_precision(ranked, relevant))
        per_metric["ndcg@10"].append(ndcg_at_k(ranked, relevant))
        per_metric["mrr@10"].append(mrr_at_k(ranked, relevant))
        per_metric["recall@10"].append(recall_at_k(ranked, relevant))
    return {
        metric: round(sum(values) / len(values), 4)
        for metric, values in per_metric.items()
    }


# --------------------------------------------------------------------------- #
# modes
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="C-MTEB T2Reranking pool evaluation")
    parser.add_argument("--limit-queries", type=int, default=0)
    parser.add_argument("--rerank-limit", type=int, default=0,
                        help="cap the (slow, CPU) cross-encoder leg at N queries; 0 = all")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    import numpy as np

    from app.config import get_settings
    from app.llm.providers import build_reranker
    from app.retrieval.bm25 import BM25Index, reciprocal_rank_fusion

    corpus, evidence, pools = load_dataset(args.limit_queries)
    text_by_pid = {row["pid"]: row["text"] for row in corpus}
    relevant_by_query = {row["query_id"]: set(row["relevant_pids"]) for row in evidence}
    print(f"queries: {len(evidence)}, corpus: {len(corpus)}")

    # --- indexes over the full corpus ---
    from app.retrieval.bm25 import token_counts

    corpus_records = []
    for row in corpus:
        counts = token_counts(row["text"])
        corpus_records.append(
            SimpleNamespace(id=row["pid"], token_counts=counts, token_length=sum(counts.values()))
        )
    bm25_index = BM25Index(corpus_records)

    matrix = np.asarray(_embed([row["text"] for row in corpus], "corpus"), dtype=np.float32)
    row_index = {row["pid"]: i for i, row in enumerate(corpus)}
    query_matrix = np.asarray(
        _embed([row["query"] for row in evidence], "queries"), dtype=np.float32
    )

    # --- per-query pool rankings ---
    rankings = {mode: {} for mode in ("bm25", "dense", "rrf", "rerank")}
    generation_recall = {"bm25@30": [], "dense@30": [], "union@30": []}
    started = time.time()
    reranker = build_reranker(get_settings())
    print(f"reranker {reranker.name} loaded in {time.time() - started:.0f}s")

    for position, row in enumerate(evidence):
        query_id = row["query_id"]
        pool = [pid for pid in pools[query_id] if pid in row_index]
        relevant = relevant_by_query[query_id]
        pool_set = set(pool)

        pool_bm25 = [pid for pid, _ in bm25_index.search(row["query"], top_k=1000) if pid in pool_set]
        missing = [pid for pid in pool if pid not in set(pool_bm25)]  # zero-BM25 tail
        pool_bm25.extend(missing)

        scores = matrix @ query_matrix[position]
        pool_dense = sorted(pool, key=lambda pid: -scores[row_index[pid]])

        fused = reciprocal_rank_fusion([pool_dense, pool_bm25], weights=[1.0, 0.8])
        pool_rrf = [pid for pid, _ in fused]

        rankings["bm25"][query_id] = pool_bm25
        rankings["dense"][query_id] = pool_dense
        rankings["rrf"][query_id] = pool_rrf

        generation_recall["bm25@30"].append(
            len(set(pool_bm25[:30]) & relevant) / len(relevant)
        )
        generation_recall["dense@30"].append(
            len(set(pool_dense[:30]) & relevant) / len(relevant)
        )
        generation_recall["union@30"].append(
            len((set(pool_dense[:30]) | set(pool_bm25[:30])) & relevant) / len(relevant)
        )

        if args.rerank_limit == 0 or position < args.rerank_limit:
            order, _ = reranker.rerank(row["query"], [text_by_pid[pid] for pid in pool], len(pool))
            rankings["rerank"][query_id] = [pool[i] for i in order]
        if (position + 1) % 50 == 0:
            print(f"  {position + 1}/{len(evidence)} queries ranked", flush=True)

    results = {
        mode: evaluate(rankings[mode], relevant_by_query)
        for mode in ("bm25", "dense", "rrf", "rerank")
        if rankings[mode]
    }
    generation = {
        name: round(sum(values) / len(values), 4)
        for name, values in generation_recall.items()
    }

    print()
    print("=" * 64)
    print(f"{'mode':<10} {'MAP':>8} {'NDCG@10':>9} {'MRR@10':>8} {'R@10':>8}")
    for mode, stats in results.items():
        print(
            f"{mode:<10} {stats['map']:>8.4f} {stats['ndcg@10']:>9.4f} "
            f"{stats['mrr@10']:>8.4f} {stats['recall@10']:>8.4f}"
        )
    print("-" * 64)
    print(f"候选生成召回（top-30 内命中的正相关例比例）: {generation}")
    print("=" * 64)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "dataset": {
                        "name": "C-MTEB T2Reranking (dev, official candidate pools)",
                        "source": "T2Ranking, arXiv:2304.03679; pools rebuilt from the official dev parquet",
                        "queries": len(evidence),
                        "passages": len(corpus),
                        "pool_size": {
                            "min": min(len(p) for p in pools.values()),
                            "p50": sorted(len(p) for p in pools.values())[len(pools) // 2],
                            "max": max(len(p) for p in pools.values()),
                        },
                    },
                    "rerank_leg_queries": len(rankings["rerank"]),
                    "results": results,
                    "generation_recall_top30": generation,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
