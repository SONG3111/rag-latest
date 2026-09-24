"""RAGFlow-style auto-keyword annotation A/B on the CRUD-RAG corpus.

Research round 3 flagged one cheap, production-proven trick we don't do yet:
RAGFlow's knowledge base has an "auto keyword" option that extracts a few
TF-IDF keywords per chunk and appends them to the indexed text, so a chunk
matches queries whose words it only implies (ragflow.io/docs,
infiniflow/ragflow — the feature is also a close cousin of Anthropic's
"contextual retrieval", minus the per-chunk LLM call).

This script measures exactly that, on the authoritative corpus, with the
production chunker and the production BM25 leg (jieba bigram index):

* ``baseline`` — child text as the pipeline indexes it today;
* ``kw3`` / ``kw5`` — child text + ``\\n关键词：k1, k2[, k3…]`` where the
  keywords come from ``jieba.analyse.extract_tags`` (TF-IDF) over the child's
  own body, mirroring RAGFlow's approach.

Grading is file-level HitRate@k / MRR@10 over the 86 labeled CRUD-RAG queries,
identical to ``eval_crud_rag.py``, so the numbers are directly comparable.
Dense-leg annotation effects need a re-embedding pass and are out of scope
here; this A/B isolates the *sparse* leg, which is where keyword injection
acts first.

Zero model calls — jieba + BM25 only.

Usage:
    python scripts/eval_keyword_annotation.py --out docs/rag-test-report/data/keyword-ab.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai-service"))
sys.path.insert(0, str(ROOT / "scripts"))

KEYWORD_MARK = "\n关键词："


def annotate(text: str, top_k: int) -> str:
    """Append TF-IDF keywords extracted from the chunk's own body.

    The ``文件：…`` header line is stripped before extraction so file ids never
    leak into the keyword list; jieba's default pretrained IDF does the scoring
    (the same trade-off RAGFlow makes: corpus-free, deterministic, cheap).
    """
    import jieba.analyse

    body = text.split(KEYWORD_MARK)[0]
    lines = body.splitlines()
    body = "\n".join(line for line in lines if not line.startswith("文件："))
    keywords = jieba.analyse.extract_tags(body, topK=top_k)
    if not keywords:
        return text
    return text + KEYWORD_MARK + ",".join(keywords)


def main() -> int:
    parser = argparse.ArgumentParser(description="keyword-annotation A/B on CRUD-RAG")
    parser.add_argument("--size", type=int, default=512)
    parser.add_argument("--overlap", type=int, default=64)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    from eval_crud_rag import (
        _dedupe,
        build_index,
        load_evidence,
        search_bm25,
        wrap_corpus,
    )

    from app.retrieval.bm25 import BM25Index, token_counts

    queries = load_evidence()
    docx_files = wrap_corpus()
    children, parents = build_index(docx_files, size=args.size, overlap=args.overlap)
    print(f"corpus: {len(docx_files)} docs -> {len(children)} children @ {args.size}x{args.overlap}")

    variants = {"baseline": children}
    for top_k in (3, 5):
        started = time.time()
        annotated = []
        for child in children:
            text = annotate(child.text, top_k)
            counts = token_counts(text)
            annotated.append(
                type(child)(
                    id=child.id,
                    text=text,
                    rel_path=child.rel_path,
                    location=child.location,
                    parent_index=child.parent_index,
                    token_counts=counts,
                    token_length=sum(counts.values()),
                )
            )
        variants[f"kw{top_k}"] = annotated
        print(f"kw{top_k}: annotated {len(annotated)} children in {time.time() - started:.0f}s")

    results = {}
    for name, records in variants.items():
        index = BM25Index(records)
        by_id = {child.id: child for child in records}
        ks = (1, 3, 5, 10)
        hits = {k: 0 for k in ks}
        rr_sum = 0.0
        misses = []
        for query in queries:
            ranked = _dedupe(search_bm25(index, records, query.query, 20), by_id)
            rank = 0
            for position, chunk_id in enumerate(ranked, start=1):
                if by_id[chunk_id].rel_path == query.label_file:
                    rank = position
                    break
            for k in ks:
                hits[k] += int(0 < rank <= k)
            rr_sum += 1.0 / rank if rank else 0.0
            if rank != 1:
                misses.append({"query": query.query, "rank": rank or None})
        total = len(queries)
        results[name] = {
            **{f"hit@{k}": round(hits[k] / total, 4) for k in ks},
            "mrr@10": round(rr_sum / total, 4),
            "not_rank1": len(misses),
            "misses": misses[:8],
        }
        print(
            f"{name:<9} hit@1={results[name]['hit@1']:.4f} hit@5={results[name]['hit@5']:.4f} "
            f"mrr@10={results[name]['mrr@10']:.4f} 非top1题数={results[name]['not_rank1']}"
        )

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "config": {"chunk": f"{args.size}x{args.overlap}", "leg": "bm25"},
                    "queries": len(queries),
                    "variants": results,
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
