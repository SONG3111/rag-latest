"""End-to-end answer quality on CRUD-RAG: real LLM generation graded by ROUGE-L.

Retrieval metrics (``eval_crud_rag.py``) tell you whether the evidence surfaced;
they cannot tell you whether the *answer* got better. This script closes the loop
with the production chain — retrieve (BM25-only vs full hybrid+rerank) → build the
context block exactly like ``format_context`` → answer with the production chat
model (qwen3.8-flash via DashScope, temperature 0) → grade the generated answer
against CRUD-RAG's reference answer with ROUGE-L F1.

ROUGE-L vs the reference answer is CRUD-RAG's own official generation metric
(BLEU/ROUGE-L/bertScore in the paper), so the numbers stay comparable to the
benchmark's protocol. Reference answers are LLM-written summaries, which makes
ROUGE-L a *relative* signal — good for A/B comparisons, not an absolute score.

Requires DASHSCOPE_API_KEY (real model calls, explicitly authorized for this
evaluation); everything else runs on local weights.

Usage:
    python scripts/eval_crud_answers.py --out docs/rag-test-report/data/crud-answers-eval.json
    python scripts/eval_crud_answers.py --limit-queries 8   # smoke run
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

_rerank_lock = threading.Lock()

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai-service"))
sys.path.insert(0, str(ROOT / "scripts"))

PROMPT = """你是一个严谨的中文问答助手。只根据下面的资料回答问题；如果资料里没有答案，就回答"资料中没有相关信息"。

资料：
{context}

问题：{question}

要求：用一句简短的中文陈述句回答，不要展开解释，不要引用资料编号。"""


# --------------------------------------------------------------------------- #
# ROUGE-L (character-level LCS, the standard sentence-level variant)
# --------------------------------------------------------------------------- #
def _lcs(a: list[str], b: list[str]) -> int:
    if not a or not b:
        return 0
    previous = [0] * (len(b) + 1)
    for i in range(1, len(a) + 1):
        current = [0]
        for j in range(1, len(b) + 1):
            if a[i - 1] == b[j - 1]:
                current.append(previous[j - 1] + 1)
            else:
                current.append(max(previous[j], current[-1]))
        previous = current
    return previous[-1]


def rouge_l_f1(hypothesis: str, reference: str) -> float:
    """ROUGE-L F1 between the generated and reference answers."""
    hyp = list(hypothesis.strip())
    ref = list(reference.strip())
    if not hyp or not ref:
        return 0.0
    lcs = _lcs(hyp, ref)
    if not lcs:
        return 0.0
    precision = lcs / len(hyp)
    recall = lcs / len(ref)
    return 2 * precision * recall / (precision + recall)


# --------------------------------------------------------------------------- #
# context construction (mirrors pipeline.format_context)
# --------------------------------------------------------------------------- #
def build_context(hits: list[tuple["Child", "Parent"]]) -> str:
    blocks = []
    for index, (child, parent) in enumerate(hits, start=1):
        blocks.append(f"[资料{index}] 文件：{child.rel_path} · 位置：{child.location}\n{parent.text}")
    return "\n\n".join(blocks)


def main() -> int:
    parser = argparse.ArgumentParser(description="CRUD-RAG end-to-end answer evaluation")
    parser.add_argument("--limit-queries", type=int, default=0)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    from eval_crud_rag import (
        Child,
        Parent,  # noqa: F401 — re-exported for type hints above
        embed_children,
        embed_queries,
        load_evidence,
        search_bm25,
        search_dense,
        wrap_corpus,
        _dedupe,
    )

    from app.config import get_settings
    from app.llm.providers import build_chat_model, build_reranker
    from app.retrieval.bm25 import BM25Index, reciprocal_rank_fusion

    production = get_settings()
    size, overlap = production.chunk_size_tokens, production.chunk_overlap_tokens

    queries = load_evidence()
    if args.limit_queries:
        queries = queries[: args.limit_queries]
    docx_files = wrap_corpus()
    print(f"production config {size}x{overlap}; queries: {len(queries)}")

    children, parents = build_index_safe(docx_files, size, overlap)
    by_id = {child.id: child for child in children}
    bm25_index = BM25Index(children)
    matrix = embed_children(children, size=size, overlap=overlap)
    query_vecs = embed_queries(queries)

    def retrieve(query: str, position: int, mode: str, top_k: int = 5):
        bm25_ids = _dedupe(search_bm25(bm25_index, children, query, 20), by_id)
        if mode == "bm25":
            ids = bm25_ids[:top_k]
        else:
            dense_ids = _dedupe(search_dense(matrix, query_vecs[position], children, 20), by_id)
            fused = reciprocal_rank_fusion([dense_ids, bm25_ids], weights=[1.0, 0.8])
            pool = [chunk_id for chunk_id, _ in fused][: 20]
            if mode == "rerank":
                # torch inference from several threads is not contractually safe;
                # the lock only serializes a CPU-bound call the GIL can't parallelize.
                with _rerank_lock:
                    order, _ = reranker.rerank(query, [by_id[cid].text for cid in pool], len(pool))
                pool = [pool[i] for i in order]
            ids = pool[:top_k]
        return [(by_id[cid], parents[by_id[cid].parent_index]) for cid in ids]

    reranker = build_reranker(production)
    chat = build_chat_model(production, temperature=0)

    def answer_one(row):
        position, query = row
        entry = {"query_id": query.query_id, "query": query.query, "reference": query.answer}
        for mode in ("bm25", "rerank"):
            started = time.time()
            hits = retrieve(query.query, position, mode)
            context = build_context(hits)
            try:
                message = chat.invoke(
                    PROMPT.format(context=context, question=query.query)
                )
                generated = message.content.strip()
                error = None
            except Exception as exc:  # noqa: BLE001 — record and keep grading the rest
                generated = ""
                error = str(exc)[:300]
            entry[mode] = {
                "answer": generated,
                "rouge_l": round(rouge_l_f1(generated, query.answer), 4),
                "latency": round(time.time() - started, 2),
                "error": error,
                "files": [child.rel_path for child, _ in hits],
            }
        print(
            f"  {query.query_id} bm25={entry['bm25']['rouge_l']:.3f} "
            f"rerank={entry['rerank']['rouge_l']:.3f}",
            flush=True,
        )
        return entry

    rows = list(enumerate(queries))
    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for entry in pool.map(answer_one, rows):
            results.append(entry)

    def summarize(mode: str) -> dict:
        scores = [row[mode]["rouge_l"] for row in results]
        errors = sum(1 for row in results if row[mode]["error"])
        return {
            "rouge_l_mean": round(sum(scores) / len(scores), 4),
            "rouge_l_p50": round(sorted(scores)[len(scores) // 2], 4),
            "errors": errors,
            "latency_mean": round(
                sum(row[mode]["latency"] for row in results) / len(results), 2
            ),
        }

    bm25_summary, rerank_summary = summarize("bm25"), summarize("rerank")
    wins = sum(
        1 for row in results if row["rerank"]["rouge_l"] > row["bm25"]["rouge_l"]
    )
    losses = sum(
        1 for row in results if row["rerank"]["rouge_l"] < row["bm25"]["rouge_l"]
    )

    print()
    print("=" * 64)
    print(f"BM25-only   ROUGE-L mean={bm25_summary['rouge_l_mean']}")
    print(f"hybrid+重排  ROUGE-L mean={rerank_summary['rouge_l_mean']}")
    print(f"重排胜/负/平: {wins}/{losses}/{len(results) - wins - losses}")
    print("=" * 64)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "config": {"chunk": f"{size}x{overlap}", "chat_model": production.llm_model},
                    "queries": len(results),
                    "summary": {"bm25": bm25_summary, "rerank": rerank_summary, "wins": wins, "losses": losses},
                    "results": results,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"written: {args.out}")
    return 0


def build_index_safe(docx_files, size, overlap):
    """Thin indirection so the module-level import stays lazy and cache-aware."""
    from eval_crud_rag import build_index

    return build_index(docx_files, size=size, overlap=overlap)


if __name__ == "__main__":
    raise SystemExit(main())
