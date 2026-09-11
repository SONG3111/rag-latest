"""Verify the locally downloaded embedding and reranking models actually load.

Run this after pointing `EMBEDDING_LOCAL_PATH` / `RERANKER_LOCAL_PATH` at your own
weights. It checks dimensions, ordering sanity, and reports load and inference
latency so the numbers in the README are reproducible.

    python scripts/check_local_models.py
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))


def main() -> int:
    from app.config import get_settings
    from app.llm.providers import ProviderError, build_embeddings, build_reranker

    settings = get_settings()
    print(f"embedding provider : {settings.embedding_provider}")
    print(f"embedding source   : {settings.embedding_local_path or settings.embedding_model}")
    print(f"reranker provider  : {settings.reranker_provider}")
    print(f"reranker source    : {settings.reranker_local_path or settings.reranker_model}")
    print()

    failures = 0

    try:
        started = time.time()
        embeddings = build_embeddings(settings)
        vectors = embeddings.embed_documents(
            ["单笔报销金额不得超过 5000 元", "差旅费报销标准与审批流程"]
        )
        elapsed = time.time() - started
        dimension = len(vectors[0])
        print(f"embedding  : OK  dim={dimension}  vectors={len(vectors)}  {elapsed:.1f}s")
        if dimension != settings.embedding_dimensions:
            print(
                f"  ! dimension mismatch: model returns {dimension} but "
                f"EMBEDDING_DIMENSIONS={settings.embedding_dimensions}. "
                "Qdrant collections are created with the configured size, so these "
                "must agree or upserts will fail."
            )
            failures += 1
    except ProviderError as exc:
        print(f"embedding  : FAIL  {exc}")
        failures += 1
    except Exception as exc:
        print(f"embedding  : FAIL  {type(exc).__name__}: {exc}")
        failures += 1

    try:
        started = time.time()
        reranker = build_reranker(settings)
        documents = [
            "第三条 出差补贴标准为每日 200 元。",
            "第二条 单笔报销金额不得超过 5000 元，超出部分需总经理审批。",
            "第五条 报销时须附发票原件、费用明细清单及对应的审批记录。",
        ]
        order = reranker.rerank("单笔报销金额的上限是多少", documents, top_n=3)
        elapsed = time.time() - started
        print(f"reranker   : OK  order={order}  {elapsed:.1f}s")
        if order and "5000" not in documents[order[0]]:
            print("  ! the reranker did not rank the governing clause first")
            failures += 1
        else:
            print("  top-1 命中正确段落")
    except ProviderError as exc:
        print(f"reranker   : FAIL  {exc}")
        failures += 1
    except Exception as exc:
        print(f"reranker   : FAIL  {type(exc).__name__}: {exc}")
        failures += 1

    print()
    print("RESULT:", "PASS" if failures == 0 else f"FAIL ({failures} problem(s))")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
