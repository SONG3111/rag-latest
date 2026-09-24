"""Local embedding and reranking providers.

These tests load real model weights from disk, so they are marked ``slow`` and are
skipped automatically when the configured paths are absent. They exist because the
local path is a first-class deployment option, not a fallback: if the weights or the
dimension contract drift, indexing silently produces unusable vectors.

Run explicitly with:  pytest -m slow
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import get_settings

pytestmark = pytest.mark.slow


def _require_local_paths() -> tuple[Path, Path]:
    settings = get_settings()
    embedding_path = Path(settings.embedding_local_path or "")
    reranker_path = Path(settings.reranker_local_path or "")
    if not embedding_path.is_dir():
        pytest.skip(f"local embedding weights not found at {embedding_path}")
    if not reranker_path.is_dir():
        pytest.skip(f"local reranker weights not found at {reranker_path}")
    return embedding_path, reranker_path


def test_local_embeddings_load_and_match_configured_dimensions() -> None:
    _require_local_paths()
    settings = get_settings()

    from app.llm.providers import build_embeddings

    embeddings = build_embeddings(settings)
    vectors = embeddings.embed_documents(["报销制度", "差旅费标准"])
    vectors.append(embeddings.embed_query("办公用品限额"))

    assert len(vectors) == 3
    for vector in vectors:
        # The collection is created with this size, so a mismatch would make every
        # upsert fail rather than degrade quietly.
        assert len(vector) == settings.embedding_dimensions


def test_local_embeddings_rank_related_text_closer() -> None:
    _require_local_paths()

    from app.llm.providers import build_embeddings

    embeddings = build_embeddings()
    query = embeddings.embed_query("单笔报销金额的上限是多少")
    related = embeddings.embed_query("第二条 单笔报销金额不得超过 5000 元")
    unrelated = embeddings.embed_query("第五条 报销时须附发票原件")

    def cosine(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right))

    assert cosine(query, related) > cosine(query, unrelated)


def test_local_reranker_puts_the_governing_clause_first() -> None:
    _require_local_paths()

    from app.llm.providers import build_reranker

    reranker = build_reranker()
    documents = [
        "第三条 出差补贴标准为每日 200 元。",
        "第二条 单笔报销金额不得超过 5000 元，超出部分需总经理审批。",
        "第五条 报销时须附发票原件、费用明细清单及对应的审批记录。",
    ]
    order, scores = reranker.rerank("单笔报销金额的上限是多少", documents, top_n=3)

    assert order[0] == 1
    assert sorted(order) == [0, 1, 2]
    # A cross-encoder must report calibrated relevance, because the retrieval layer
    # gates on these numbers.
    assert reranker.produces_relevance_scores is True
    assert len(scores) == len(order)
    assert scores[0] > 0.5


def test_local_reranker_handles_empty_input() -> None:
    _require_local_paths()

    from app.llm.providers import build_reranker

    assert build_reranker().rerank("任意问题", [], top_n=5) == ([], [])


def test_noop_reranker_preserves_order() -> None:
    """The degradation path must never drop or reorder candidates unexpectedly."""
    from app.llm.providers import NoopReranker

    reranker = NoopReranker()
    order, scores = reranker.rerank("q", ["a", "b", "c"], top_n=2)
    assert order == [0, 1]
    # No cross-encoder, so no relevance scale, so the gate must stay off.
    assert reranker.produces_relevance_scores is False
    assert scores == [0.0, 0.0]


def test_noop_reranker_passes_fused_scores_through() -> None:
    from app.llm.providers import NoopReranker

    order, scores = NoopReranker().rerank("q", ["a", "b"], top_n=2, fused_scores=[0.5, 0.25])
    assert order == [0, 1]
    assert scores == [0.5, 0.25]


def test_body_splitter_measures_with_the_embedding_tokenizer() -> None:
    """Body chunks are budgeted by the same tokenizer the vector store encodes with."""
    _require_local_paths()

    from app.retrieval.chunking import build_body_splitter

    settings = get_settings()
    splitter = build_body_splitter(
        128, 16, model_path=str(Path(settings.embedding_local_path))
    )

    text = "员工报销单据应当在费用发生后十个工作日内提交。" * 12
    measured = splitter.measure(text)
    # bge-m3 often merges common Chinese words below one token per character,
    # but 276 characters still overshoots a 128-token budget many times over.
    assert 128 < measured <= len(text)

    pieces = splitter.splitter.split_text(text)
    assert len(pieces) > 1
    assert all(splitter.measure(piece) <= 128 for piece in pieces)
    assert all(piece.endswith("。") for piece in pieces)
