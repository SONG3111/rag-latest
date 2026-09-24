"""Retrieval quality evaluation.

RAG systems are usually demoed with one happy-path question, which proves nothing.
This runs a labeled set across four categories and reports the metrics that actually
distinguish a retrieval improvement from noise:

* **Recall@K / MRR** over questions the corpus *can* answer.
* **Refusal accuracy** over questions it *cannot*. This is the metric the relevance
  gate exists for, and it is the one a naive pipeline fails outright — it returns
  five passages no matter what, so the model always has something to hallucinate from.
* **Rewrite lift**: the same colloquial questions run with rewriting off and on, so
  the contribution of that stage is measured rather than asserted.

Usage:

    python scripts/eval_retrieval.py                      # keyword only, no API key
    python scripts/eval_retrieval.py --dense              # + local embeddings & rerank
    python scripts/eval_retrieval.py --dense --rewrite    # + LLM query rewriting
    python scripts/eval_retrieval.py --dense --report out.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai-service"))
sys.path.insert(0, str(ROOT / "scripts"))


@dataclass
class Case:
    """One labeled query.

    For answerable cases ``must_contain`` is the fact the answer depends on, which
    makes grading independent of chunk boundaries. Negative cases have no expected
    content: the correct behaviour is to return nothing.
    """

    query: str
    category: str
    file: str = ""
    must_contain: str = ""
    history: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def answerable(self) -> bool:
        return bool(self.must_contain)


DOC = "员工费用报销管理制度.docx"
SHEET = "2026年第一季度报销明细.xlsx"

CASES: list[Case] = [
    # ---------- in-corpus facts (the original set) ----------
    Case("招待费单笔上限是多少", "in-corpus", DOC, "3000"),
    Case("差旅费最多能报销多少", "in-corpus", DOC, "5000"),
    Case("办公用品费的限额是多少", "in-corpus", DOC, "2000"),
    Case("培训费的限额是多少", "in-corpus", DOC, "5000"),
    Case("单笔超过多少钱需要总经理审批", "in-corpus", DOC, "5000"),
    Case("报销需要提交哪些材料", "in-corpus", DOC, "发票"),
    Case("报销申请要在多少天内提交", "in-corpus", DOC, "30"),
    Case("本制度从什么时候开始施行", "in-corpus", DOC, "2026"),
    Case("哪个部门负责解释这份制度", "in-corpus", DOC, "财务部"),
    Case("制度里有哪些费用类型", "in-corpus", DOC, "差旅费"),
    Case("张伟报销了多少钱", "in-corpus", SHEET, "3200"),
    Case("李娜的报销金额是多少", "in-corpus", SHEET, "8600"),
    Case("谁提交了招待费", "in-corpus", SHEET, "李娜"),
    Case("技术部有哪些报销记录", "in-corpus", SHEET, "技术部"),
    Case("哪些报销单还在待审批", "in-corpus", SHEET, "待审批"),
    Case("BX-2026-005 这笔报销的金额", "in-corpus", SHEET, "7200"),
    Case("赵敏提交的报销类型是什么", "in-corpus", SHEET, "办公用品"),
    Case("周杰的报销金额", "in-corpus", SHEET, "4800"),

    # ---------- colloquial phrasing: what a real user types ----------
    Case("报销咋整", "colloquial", DOC, "发票", note="期望改写到报销流程/材料"),
    Case("出差花销最多报多少", "colloquial", DOC, "5000"),
    Case("请客吃饭能报几个钱", "colloquial", DOC, "3000"),
    Case("这表里张伟那笔多少钱", "colloquial", SHEET, "3200"),
    Case("谁还没给批啊", "colloquial", SHEET, "待审批"),
    Case("买文具能报多少", "colloquial", DOC, "2000"),

    # ---------- out-of-corpus: the correct answer is "not found" ----------
    Case("如何申请公司年假", "negative", note="制度文档里没有年假"),
    Case("公司的股权激励计划怎么算", "negative"),
    Case("会议室投影仪怎么连接", "negative"),
    Case("竞争对手去年的营收是多少", "negative"),
    Case("我的社保缴费基数是多少", "negative"),
    Case("团建活动的预算标准", "negative"),
    Case("公司班车几点发车", "negative"),
    Case("年终奖怎么计算", "negative"),

    # ---------- multi-turn ellipsis: only resolvable with history ----------
    Case(
        "那第三条呢",
        "multi-turn",
        DOC,
        "30",
        history=["用户: 报销制度第二条讲的是什么", "助手: 第二条是关于报销限额的规定。"],
        note="第三条是提交时限",
    ),
    Case(
        "它的上限是多少",
        "multi-turn",
        DOC,
        "3000",
        history=["用户: 招待费怎么规定的", "助手: 招待费属于报销范围。"],
    ),
    Case(
        "那这个人呢",
        "multi-turn",
        SHEET,
        "8600",
        history=["用户: 张伟报销了多少钱", "助手: 张伟报销了 3200 元。"],
        note="指代需要从表格里推断",
    ),
    Case(
        "刚才说的那个限额，办公用品也一样吗",
        "multi-turn",
        DOC,
        "2000",
        history=["用户: 差旅费的上限是多少", "助手: 差旅费单笔不得超过 5000 元。"],
    ),
]


def _evaluate_case(retriever, case: Case, args, *, threshold: float | None) -> dict:
    """Run one case and grade it.

    Answerable cases are graded on whether the expected fact appears in the returned
    context, which is what the model actually reads. Negative cases are graded on the
    gate leaving nothing to cite — returning *any* passage for a question the corpus
    cannot answer is the failure this metric exists to catch.
    """
    results = retriever.search(
        case.query,
        top_k=args.top_k,
        use_dense=args.dense,
        use_rerank=args.dense,
        use_rewrite=args.rewrite,
        history=case.history,
        relevance_threshold=threshold,
    )
    run = retriever.last_run

    rank = 0
    if case.answerable:
        for index, item in enumerate(results, start=1):
            # Grade against the parent context too: that is the text the model sees.
            if item.rel_path == case.file and case.must_contain in item.context_text:
                rank = index
                break
        correct = rank > 0
    else:
        correct = len(results) == 0

    return {
        "query": case.query,
        "category": case.category,
        "answerable": case.answerable,
        "correct": correct,
        "rank": rank or None,
        "rewritten": run.rewritten,
        "effective_query": run.effective_query,
        "filtered_by_threshold": run.filtered_by_threshold,
        "returned": [
            {
                "file": item.rel_path,
                "location": item.location,
                "score": item.score,
                "score_source": item.score_source,
                "context_text": item.context_text,
            }
            for item in results
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate retrieval on the demo corpus")
    parser.add_argument("--dense", action="store_true", help="use vector search and reranking")
    parser.add_argument("--rewrite", action="store_true", help="enable LLM query rewriting")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--threshold", type=float, default=None, help="override the relevance floor")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="report recall/refusal across a range of thresholds instead of one",
    )
    args = parser.parse_args()

    from make_demo_data import build_document, build_workbook

    from app.config import get_settings
    from app.retrieval.bm25 import token_counts
    from app.retrieval.chunking import chunk_document_groups
    from app.retrieval.pipeline import ChunkRecord, Retriever

    settings = get_settings()
    if args.rewrite and not settings.dashscope_api_key:
        print("! --rewrite requires DASHSCOPE_API_KEY; rewriting will fall back silently\n")

    tmp = Path(tempfile.mkdtemp(prefix="rag-eval-"))
    original_data_dir = settings.data_dir
    settings.data_dir = tmp / "data"
    settings.ensure_directories()

    try:
        # 语料构造走 corpus_loader 的内存模式（与 tests/test_retrieval.py::_corpus
        # 相同）：chunk 的持久化归 backend-java，评测只关心 (files, chunks) 形状。
        corpus_dir = tmp / "corpus"
        build_workbook(corpus_dir / SHEET)
        build_document(corpus_dir / DOC)

        file_ids = {SHEET: "f-sheet", DOC: "f-doc"}
        records: list[ChunkRecord] = []
        for path in sorted(corpus_dir.iterdir()):
            rel_path = path.name
            file_id = file_ids[rel_path]
            groups = chunk_document_groups(path, rel_path)
            if not groups:
                print(f"warning: {rel_path} produced no chunks")
            for ordinal, group in enumerate(groups):
                parent_id = f"{file_id}-p{ordinal}"
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
                    records.append(
                        ChunkRecord(
                            id=(
                                f"{parent_id}-"
                                f"{child.meta.get('row', child.meta.get('part', 0))}"
                            ),
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
        corpus = ({file_id: rel for rel, file_id in file_ids.items()}, records)

        retriever = Retriever("eval", corpus_loader=lambda: corpus)

        rows: list[dict] = []
        by_category: dict[str, list[dict]] = {}

        # The threshold sweep reuses one ungated retrieval per case and filters the
        # result in memory. Re-running retrieval per threshold would cost ~8x the
        # model calls to compute exactly the same numbers.
        sweep_values = (
            [0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.08, 0.12] if args.sweep else []
        )
        if sweep_values:
            ungated = [
                _evaluate_case(retriever, case, args, threshold=0.0) for case in CASES
            ]
            print("阈值扫描（同一批检索结果，按不同阈值过滤）")
            print(f"{'threshold':>10} {'recall':>10} {'refusal':>10} {'kept':>7}")
            print("-" * 42)
            for candidate in sweep_values:
                answerable_hits = 0
                answerable_total = 0
                refused = 0
                negative_total = 0
                kept = 0
                for case, row in zip(CASES, ungated):
                    survivors = [
                        item
                        for item in row["returned"]
                        if item["score_source"] != "rerank" or item["score"] >= candidate
                    ]
                    kept += len(survivors)
                    if case.answerable:
                        answerable_total += 1
                        if any(
                            item["file"] == case.file
                            and case.must_contain in item["context_text"]
                            for item in survivors
                        ):
                            answerable_hits += 1
                    else:
                        negative_total += 1
                        if not survivors:
                            refused += 1
                hit_rate = answerable_hits / answerable_total if answerable_total else 0.0
                refusal_rate = refused / negative_total if negative_total else 0.0
                print(
                    f"{candidate:>10.3f} {hit_rate:>10.4f} {refusal_rate:>10.4f} {kept:>7}"
                )
            print()

        for case in CASES:
            record = _evaluate_case(retriever, case, args, threshold=args.threshold)
            rows.append(record)
            by_category.setdefault(case.category, []).append(record)

            rank = record["rank"]
            correct = record["correct"]
            marker = "✓" if correct else "✗"
            position = f"#{rank}" if rank else ("refused" if not case.answerable else "miss")
            rewrite_note = (
                "→" + record["effective_query"] if record["rewritten"] else ""
            )
            print(f"{marker} {position:>8}  {case.query} {rewrite_note}")

        # ------------------------------------------------------------------ #
        # aggregate
        # ------------------------------------------------------------------ #
        answerable = [row for row in rows if row["answerable"]]
        negatives = [row for row in rows if not row["answerable"]]

        hits = sum(1 for row in answerable if row["correct"])
        recall = hits / len(answerable) if answerable else 0.0
        mrr = (
            sum((1.0 / row["rank"]) if row["rank"] else 0.0 for row in answerable)
            / len(answerable)
            if answerable
            else 0.0
        )
        refusals = sum(1 for row in negatives if row["correct"])
        refusal_accuracy = refusals / len(negatives) if negatives else 0.0
        rewrites = sum(1 for row in rows if row["rewritten"])
        rewrite_rate = rewrites / len(rows) if rows else 0.0

        mode_parts = ["BM25"]
        if args.dense:
            mode_parts = ["BM25", "向量", "重排"]
        mode = " + ".join(mode_parts)
        if args.rewrite:
            mode += " + 查询改写"
        threshold = (
            args.threshold
            if args.threshold is not None
            else settings.rerank_score_threshold
        )
        gate = f"{threshold}" if args.dense else "未启用（无重排分数）"

        print()
        print("=" * 68)
        print(f"检索模式          : {mode}")
        print(f"相关性阈值         : {gate}")
        print(f"总问题数          : {len(rows)}")
        print(f"Recall@{args.top_k:<11}: {recall:.4f}  ({hits}/{len(answerable)})")
        print(f"MRR               : {mrr:.4f}")
        print(f"拒答准确率         : {refusal_accuracy:.4f}  ({refusals}/{len(negatives)})")
        print(f"查询改写触发率      : {rewrite_rate:.4f}  ({rewrites}/{len(rows)})")
        print("-" * 68)
        for category, items in by_category.items():
            correct = sum(1 for item in items if item["correct"])
            print(f"  {category:<12} {correct}/{len(items)}")
        print("=" * 68)

        if args.report:
            args.report.write_text(
                json.dumps(
                    {
                        "mode": mode,
                        "top_k": args.top_k,
                        "threshold": threshold if args.dense else None,
                        "cases": len(rows),
                        "recall": recall,
                        "mrr": mrr,
                        "refusal_accuracy": refusal_accuracy,
                        "rewrite_rate": rewrite_rate,
                        "by_category": {
                            name: {
                                "total": len(items),
                                "correct": sum(1 for item in items if item["correct"]),
                            }
                            for name, items in by_category.items()
                        },
                        "results": rows,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            print(f"详细结果已写入 {args.report}")

        return 0
    finally:
        settings.data_dir = original_data_dir


if __name__ == "__main__":
    raise SystemExit(main())
