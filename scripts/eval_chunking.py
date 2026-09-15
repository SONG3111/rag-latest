"""Chunking-strategy evaluation over a composite, authoritative corpus.

Chunking parameters are usually copy-pasted (512/64 from a blog post). This
script measures them instead, over three complementary corpora:

* **CMRC 2018** (ymcui/cmrc2018, EMNLP 2019, CC BY-SA 4.0) — an authoritative
  public Chinese reading-comprehension set. Contexts become Word sections; the
  extractive answers become grading spans. Two flavours: standard sections
  (one context each) and *pressure* sections (five contexts fused into one
  long paragraph, so the splitter must actually fire).
* **业务仿真集** — the demo policy docx + expense xlsx plus their curated
  labeled cases (reused from ``eval_retrieval``), covering the office-document
  shapes this project actually serves. A larger 60-row sheet drives the
  ``rows_per_parent`` sweep.
* **对抗结构集** — stacked tables, a wide sheet, offset data, oversized
  paragraphs; scored structurally (budget compliance, mid-sentence cuts)
  rather than by retrieval.

Retrieval is BM25-only (``use_dense=False, use_rerank=False``), so the whole
sweep runs offline with zero model calls; only the *chunker* varies. A fast
in-memory path replays the production BM25 leg (same chunkers, same
``token_counts``, same ``BM25Index``, same fingerprint dedupe), and an
equivalence check asserts it matches the real ``Retriever`` on a sample.

Sampling is deterministic via SHA-256 keys (no RNG), so every run builds the
identical corpus from the identical CMRC snapshot.

Usage:

    python scripts/eval_chunking.py                    # full grid sweep
    python scripts/eval_chunking.py --sizes 512 --overlaps 64   # single combo
    python scripts/eval_chunking.py --out docs/rag-test-report/data/chunking-sweep.json
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import socket
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

# Fixed mirrors for the CMRC 2018 dev snapshot; hosts are validated against
# DOWNLOAD_HOSTS before any request leaves the process.
CMRC_DEV_URLS = [
    "https://raw.githubusercontent.com/ymcui/cmrc2018/master/squad-style-data/cmrc2018_dev.json",
    "https://ghproxy.net/https://raw.githubusercontent.com/ymcui/cmrc2018/master/squad-style-data/cmrc2018_dev.json",
]
DOWNLOAD_HOSTS = {"raw.githubusercontent.com", "ghproxy.net"}
SEED = 20260916

STANDARD_FILES = 16
ARTICLES_PER_STANDARD_FILE = 15
PRESSURE_FILES = 12
ARTICLES_PER_PRESSURE_FILE = 5
STANDARD_CASES = 150

SHEET60_CASES = [
    # query, must_contain — graded against the 60-row synthetic workbook
    ("周数据里市场部一共有多少笔报销", "市场部"),
    ("所有差旅费的报销单状态都是什么", "差旅费"),
    ("李娜在二月份提交了几笔报销", "李娜"),
    ("哪个部门的报销金额最高", "部"),
]


def stable_float(seed_text: str) -> float:
    """Deterministic pseudo-selection in [0, 1); reproducibility, not security."""
    digest = hashlib.sha256(f"{SEED}:{seed_text}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(1 << 64)


def stable_order(items: list, key_of) -> list:
    """Deterministic shuffle: sort by per-item stable key."""
    return sorted(items, key=lambda item: (stable_float(key_of(item)), key_of(item)))


@dataclass
class Case:
    query: str
    file: str
    must_contain: str
    category: str


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
    meta: dict = field(default_factory=dict)


@dataclass
class Parent:
    text: str
    location: str
    rel_path: str


# --------------------------------------------------------------------------- #
# corpus construction
# --------------------------------------------------------------------------- #
def _validate_download_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https":
        raise ValueError(f"only https downloads are allowed: {url}")
    if parsed.hostname not in DOWNLOAD_HOSTS:
        raise ValueError(f"host not in download allowlist: {parsed.hostname}")
    for family in (socket.getaddrinfo(parsed.hostname, 443, socket.AF_INET), socket.getaddrinfo(parsed.hostname, 443, socket.AF_INET6)):
        for entry in family:
            address = ipaddress.ip_address(entry[4][0])
            if address.is_private or address.is_loopback or address.is_link_local:
                raise ValueError(f"resolves to a non-public address: {address}")


def ensure_cmrc(cache_dir: Path) -> Path:
    target = cache_dir / "cmrc2018_dev.json"
    if target.exists() and target.stat().st_size > 1_000_000:
        return target
    cache_dir.mkdir(parents=True, exist_ok=True)
    for url in CMRC_DEV_URLS:
        try:
            _validate_download_url(url)
            print(f"downloading {url}")
            with urllib.request.urlopen(url, timeout=60) as response:
                target.write_bytes(response.read())
            return target
        except Exception as exc:  # noqa: BLE001 — try the next mirror
            print(f"  failed: {exc}")
    raise SystemExit("could not download CMRC 2018 dev set; see README")


def load_cmrc_articles(path: Path) -> list[dict]:
    """Flatten the SQuAD-style file into (context, question, answer) articles."""
    data = json.loads(path.read_text(encoding="utf-8"))["data"]
    articles = []
    for entry in data:
        paragraph = entry["paragraphs"][0]
        qas = [
            {
                "question": qa["question"],
                "answer": qa["answers"][0]["text"],
            }
            for qa in paragraph["qas"]
            if qa.get("answers") and qa["answers"][0].get("text", "").strip()
        ]
        if qas:
            articles.append({"context": paragraph["context"], "qas": qas})
    return articles


def build_cmrc_corpus(corpus_dir: Path, articles: list[dict]) -> list[Case]:
    """Write standard + pressure docx files, return their labeled cases."""
    from docx import Document

    shuffled = stable_order(articles, key_of=lambda a: a["context"][:24])

    standard_count = STANDARD_FILES * ARTICLES_PER_STANDARD_FILE
    pressure_count = PRESSURE_FILES * ARTICLES_PER_PRESSURE_FILE
    pool = shuffled[: standard_count + pressure_count]
    standard, pressure = pool[:standard_count], pool[standard_count:]

    cases: list[Case] = []

    def pick_qa(article: dict, tag: str) -> dict:
        return article["qas"][int(stable_float(f"{tag}:{article['context'][:24]}") * len(article["qas"]))]

    for file_index in range(STANDARD_FILES):
        chunk_articles = standard[
            file_index * ARTICLES_PER_STANDARD_FILE : (file_index + 1) * ARTICLES_PER_STANDARD_FILE
        ]
        name = f"资料集-{file_index + 1:02d}.docx"
        document = Document()
        document.add_heading(f"资料集 {file_index + 1}", level=1)
        for article in chunk_articles:
            title = article["context"][:16].rstrip("，。；、")
            document.add_heading(title, level=2)
            document.add_paragraph(article["context"])
        document.save(str(corpus_dir / name))

        for article in chunk_articles:
            qa = pick_qa(article, "std")
            cases.append(Case(qa["question"], name, qa["answer"], "cmrc-standard"))

    for file_index in range(PRESSURE_FILES):
        chunk_articles = pressure[
            file_index * ARTICLES_PER_PRESSURE_FILE : (file_index + 1) * ARTICLES_PER_PRESSURE_FILE
        ]
        name = f"长文汇编-{file_index + 1:02d}.docx"
        document = Document()
        document.add_heading(f"长文汇编 {file_index + 1}", level=1)
        # Five contexts fused into ONE paragraph (~2,300 chars ≈ 1,800 tokens)
        # so the token splitter must cut it, stressing boundaries and overlap.
        document.add_heading(f"第 {file_index + 1} 部分", level=2)
        document.add_paragraph("".join(a["context"] for a in chunk_articles))
        document.save(str(corpus_dir / name))

        for article in chunk_articles:
            qa = pick_qa(article, "press")
            cases.append(Case(qa["question"], name, qa["answer"], "cmrc-pressure"))

    standard_cases = [c for c in cases if c.category == "cmrc-standard"]
    pressure_cases = [c for c in cases if c.category == "cmrc-pressure"]
    ordered = stable_order(standard_cases, key_of=lambda c: c.query)
    return ordered[:STANDARD_CASES] + pressure_cases


def build_sheet60(path: Path) -> None:
    """A 60-row workbook: big enough for rows_per_parent to matter."""
    from openpyxl import Workbook

    names = ["张伟", "李娜", "王强", "赵敏", "陈晨", "刘洋", "孙婷", "周杰", "吴九", "郑十"]
    departments = ["销售部", "市场部", "技术部", "财务部"]
    types = ["差旅费", "招待费", "办公用品", "培训费"]
    statuses = ["已通过", "待审批", "已驳回"]

    book = Workbook()
    sheet = book.active
    sheet.title = "报销明细"
    sheet.append(["报销单号", "姓名", "部门", "报销类型", "金额", "提交日期", "状态"])
    for index in range(60):
        sheet.append(
            [
                f"BX-2026-{index + 1:03d}",
                names[index % len(names)],
                departments[index % len(departments)],
                types[index % len(types)],
                200 + int(stable_float(f"amount:{index}") * 8800),
                f"2026-0{1 + int(stable_float(f'month:{index}') * 3)}-{10 + int(stable_float(f'day:{index}') * 18):02d}",
                statuses[index % len(statuses)],
            ]
        )
    book.save(path)


def build_adversarial(corpus_dir: Path) -> None:
    """Structural stress documents; measured for budget compliance, not retrieval."""
    from docx import Document
    from openpyxl import Workbook

    # stacked tables separated by a blank row, each with its own header
    book = Workbook()
    sheet = book.active
    sheet.append(["产品", "类别", "数量", "单价"])
    for row in [
        ("显示器", "数码", 7, 899.0),
        ("打印机", "办公", 3, 1299.0),
    ]:
        sheet.append(list(row))
    sheet.append([])
    sheet.append(["部门", "报销人", "事由", "金额", "状态"])
    for row in [
        ("市场部", "张伟", "客户招待", 1200.0, "已审批"),
        ("技术部", "李娜", "差旅住宿", 860.0, "待审批"),
    ]:
        sheet.append(list(row))
    book.save(str(corpus_dir / "对抗-堆叠表.xlsx"))

    # wide sheet: 26 columns
    wide = Workbook()
    sheet = wide.active
    sheet.append([f"指标{chr(65 + i)}" for i in range(26)])
    for row_index in range(20):
        sheet.append([f"值{row_index}-{chr(65 + i)}" for i in range(26)])
    wide.save(str(corpus_dir / "对抗-宽表.xlsx"))

    # oversized paragraphs in one docx
    document = Document()
    document.add_heading("对抗-超长段落", level=1)
    base = (
        "报销人应当在费用发生后的三十个工作日内提交报销申请，逾期未提交的，财务部门有权拒绝受理。"
        "报销单据必须包含发票原件、费用明细清单、审批签字三部分，缺少任何一部分均视为材料不全。"
        "对于跨自然月的费用，应当按月分别提交，不得合并打包。"
    )
    document.add_paragraph(base * 8)   # ~2,400 chars
    document.add_paragraph(base * 16)  # ~4,800 chars
    document.save(str(corpus_dir / "对抗-超长段落.docx"))


def build_corpus(corpus_dir: Path) -> list[Case]:
    """Materialize every corpus file; return the labeled case list."""
    from eval_retrieval import CASES as BIZ_CASES, DOC, SHEET
    from make_demo_data import build_document, build_workbook

    build_document(corpus_dir / DOC)
    build_workbook(corpus_dir / SHEET)
    build_sheet60(corpus_dir / "2026年报销明细-大表.xlsx")
    build_adversarial(corpus_dir)

    from eval_retrieval import CASES as BIZ_CASES

    cases = [
        Case(c.query, c.file, c.must_contain, f"biz-{'doc' if c.file == DOC else 'sheet'}")
        for c in BIZ_CASES
        if c.category in {"in-corpus", "colloquial"} and c.must_contain
    ]
    cases += [
        Case(q, "2026年报销明细-大表.xlsx", span, "biz-sheet60")
        for q, span in SHEET60_CASES
    ]
    return cases


# --------------------------------------------------------------------------- #
# fast in-memory index (mirrors the production BM25-only path)
# --------------------------------------------------------------------------- #
def make_measure(model_path: str):
    """Return (BodySplitter factory, size measure) matching production mode."""
    from app.retrieval.chunking import BODY_SEPARATORS, BodySplitter

    if model_path:
        from langchain_text_splitters import RecursiveCharacterTextSplitter
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(model_path)
        measure = lambda text: len(tokenizer.encode(text))  # noqa: E731

        def factory(size: int, overlap: int) -> BodySplitter:
            splitter = RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
                tokenizer=tokenizer,
                chunk_size=size,
                chunk_overlap=overlap,
                separators=BODY_SEPARATORS,
                keep_separator="end",
            )
            return BodySplitter(splitter=splitter, measure=measure)

        return factory, measure

    from langchain_text_splitters import RecursiveCharacterTextSplitter

    measure = len

    def factory(size: int, overlap: int) -> BodySplitter:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=size,
            chunk_overlap=overlap,
            separators=BODY_SEPARATORS,
            keep_separator="end",
            length_function=len,
        )
        return BodySplitter(splitter=splitter, measure=measure)

    return factory, measure


def build_index(
    corpus_dir: Path,
    case_files: set[str],
    *,
    size: int,
    overlap: int,
    rows_per_parent: int,
    splitter_factory,
    measure,
) -> tuple[list[Child], list[Parent]]:
    """Chunk the corpus with one parameter set into lightweight records.

    Adversarial files (``对抗-*``) are chunked for structural stats but never
    become retrieval candidates, unless a case targets them.
    """
    from app.retrieval.bm25 import token_counts
    from app.retrieval.chunking import chunk_excel_groups, chunk_word_groups

    body_splitter = splitter_factory(size, overlap)
    children: list[Child] = []
    parents: list[Parent] = []

    for path in sorted(corpus_dir.iterdir()):
        rel_path = path.name
        if rel_path.startswith("对抗-") and rel_path not in case_files:
            structural_only = True
        else:
            structural_only = False
        if path.suffix.lower() in {".xlsx", ".xlsm"}:
            groups = chunk_excel_groups(
                path,
                rel_path,
                rows_per_parent=rows_per_parent,
                chunk_size_tokens=size,
                chunk_overlap_tokens=overlap,
                body_splitter=body_splitter,
            )
        elif path.suffix.lower() == ".docx":
            groups = chunk_word_groups(
                path,
                rel_path,
                chunk_size_tokens=size,
                chunk_overlap_tokens=overlap,
                body_splitter=body_splitter,
            )
        else:
            continue
        for group in groups:
            parent_index = len(parents)
            parents.append(Parent(group.parent.text, group.parent.location, rel_path))
            for ordinal, child in enumerate(group.children):
                if structural_only:
                    continue
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
                        meta=child.meta,
                    )
                )

    return children, parents


def search_bm25(
    index, children: list[Child], parents: list[Parent], query: str, *, top_k: int = 5, candidates: int = 20
) -> list[tuple[Child, Parent]]:
    """The production BM25-only leg: search, fingerprint-dedupe, truncate."""
    from app.retrieval.bm25 import content_fingerprint

    hits = index.search(query, top_k=candidates)
    by_id = {child.id: child for child in children}
    seen: set[str] = set()
    ranked: list[tuple[Child, Parent]] = []
    for chunk_id, _score in hits:
        child = by_id[chunk_id]
        fingerprint = content_fingerprint(child.text)
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        ranked.append((child, parents[child.parent_index]))
        if len(ranked) >= top_k:
            break
    return ranked


# --------------------------------------------------------------------------- #
# metrics
# --------------------------------------------------------------------------- #
SENTENCE_END = ("。", "！", "？", "；", "!", "?", ";", "：", ":", "\n")


def grade(cases: list[Case], index, children, parents) -> dict:
    per_category: dict[str, dict] = {}
    for case in cases:
        hits = search_bm25(index, children, parents, case.query)
        rank = 0
        child_hit = False
        for position, (child, parent) in enumerate(hits, start=1):
            if child.rel_path == case.file and case.must_contain in parent.text:
                rank = position
                child_hit = any(
                    c.rel_path == case.file and case.must_contain in c.text
                    for c, _ in hits
                )
                break
        bucket = per_category.setdefault(
            case.category,
            {"total": 0, "recall": 0, "ranks": [], "child_hit": 0},
        )
        bucket["total"] += 1
        if rank:
            bucket["recall"] += 1
            bucket["ranks"].append(rank)
            bucket["child_hit"] += int(child_hit)

    summary = {}
    for category, bucket in per_category.items():
        ranks = bucket["ranks"]
        summary[category] = {
            "total": bucket["total"],
            "recall@5": round(bucket["recall"] / bucket["total"], 4),
            "mrr@5": round(sum(1.0 / r for r in ranks) / bucket["total"], 4),
            "child_precision@5": round(bucket["child_hit"] / bucket["total"], 4),
        }
    return summary


def failed_cases(cases: list[Case], index, children, parents) -> list[dict]:
    """Queries with no top-5 hit carrying the expected span (for the report)."""
    failures = []
    for case in cases:
        hits = search_bm25(index, children, parents, case.query)
        if not any(
            c.rel_path == case.file and case.must_contain in p.text for c, p in hits
        ):
            failures.append(
                {"query": case.query, "file": case.file, "expect": case.must_contain}
            )
    return failures


def structural_stats(children: list[Child], parents: list[Parent], *, size: int, measure, corpus_dir: Path | None = None) -> dict:
    """Chunker health metrics that need no retrieval: budget compliance and cut quality."""
    docx_sizes = [measure(c.text) for c in children if c.rel_path.endswith(".docx")]
    excel_sizes = [measure(c.text) for c in children if c.rel_path.endswith(".xlsx")]

    # A cut is ragged when a split piece does not end on a sentence mark.
    ragged = boundaries = 0
    previous: Child | None = None
    for child in children:
        part = child.meta.get("part", 0)
        if (
            previous is not None
            and previous.parent_index == child.parent_index
            and part == previous.meta.get("part", -1) + 1
        ):
            boundaries += 1
            if not previous.text.rstrip().endswith(SENTENCE_END):
                ragged += 1
        previous = child if child.rel_path.endswith(".docx") or part else None

    def pct(values: list[int], quantile: float):
        if not values:
            return None
        ordered = sorted(values)
        return ordered[min(len(ordered) - 1, int(len(ordered) * quantile))]

    rows_per_parent: dict[int, int] = {}
    for child in children:
        if child.rel_path.endswith(".xlsx"):
            rows_per_parent[child.parent_index] = rows_per_parent.get(child.parent_index, 0) + 1

    row_counts = list(rows_per_parent.values())
    return {
        "children": len(children),
        "parents": len(parents),
        "docx_children": len(docx_sizes),
        "docx_size_p50": pct(docx_sizes, 0.5),
        "docx_size_p95": pct(docx_sizes, 0.95),
        "docx_size_max": pct(docx_sizes, 0.9999),
        "docx_over_budget_pct": round(sum(1 for v in docx_sizes if v > size) / len(docx_sizes), 4)
        if docx_sizes
        else 0.0,
        "excel_children": len(excel_sizes),
        "excel_size_p50": pct(excel_sizes, 0.5),
        "excel_size_p95": pct(excel_sizes, 0.95),
        "excel_rows_per_parent_p50": pct(row_counts, 0.5),
        "excel_rows_per_parent_max": pct(row_counts, 0.9999),
        "split_boundaries": boundaries,
        "ragged_cut_pct": round(ragged / boundaries, 4) if boundaries else 0.0,
    }


# --------------------------------------------------------------------------- #
# equivalence check against the real pipeline
# --------------------------------------------------------------------------- #
class FakeEmbeddings:
    """Deterministic vectors sized for the collection; only the BM25 leg is under test."""

    dimensions = 1024

    def _vector(self, text: str) -> list[float]:
        raw = (hashlib.sha256(text.encode("utf-8")).digest() * 64)[: self.dimensions]
        return [(b / 255.0) for b in raw]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)


def equivalence_check(
    corpus_dir: Path, cases: list[Case], *, size: int, overlap: int, model_path: str
) -> dict:
    """Run the real Retriever (BM25-only) on a sample; compare with fast path."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app import models  # noqa: F401
    from app.config import Settings
    from app.db import Base
    from app.models import Workspace
    from app.retrieval.bm25 import BM25Index
    from app.retrieval.pipeline import Retriever
    from app.services.files import index_file, save_upload

    with tempfile.TemporaryDirectory(prefix="rag-equiv-") as tmp:
        data_dir = Path(tmp) / "data"
        settings = Settings(
            data_dir=data_dir,
            embedding_provider="dashscope",
            embedding_local_path="",
            chunk_size_tokens=size,
            chunk_overlap_tokens=overlap,
            query_rewrite_enabled=False,
        )
        settings.ensure_directories()
        engine = create_engine(
            f"sqlite:///{(Path(tmp) / 'equiv.db').as_posix()}", future=True
        )
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine, expire_on_commit=False, future=True)()

        sample_names = {
            "员工费用报销管理制度.docx",
            "资料集-01.docx",
            "长文汇编-01.docx",
        }
        sample_files = sorted(
            p for p in corpus_dir.iterdir() if p.name in sample_names
        )
        workspace = Workspace(name="equiv")
        session.add(workspace)
        session.flush()
        for path in sample_files:
            record = save_upload(session, workspace, path.name, path.read_bytes())
            index_file(
                session,
                workspace.id,
                record,
                settings=settings,
                embeddings=FakeEmbeddings(),
            )
        session.flush()

        # index_file's tokenizer selection reads the *global* settings, so the
        # fast path must measure in the same mode the global settings imply.
        global_path = ""
        from app.config import get_settings as _gs

        if _gs().embedding_provider == "local" and _gs().embedding_local_path:
            global_path = _gs().embedding_local_path
        factory, measure = make_measure(global_path)
        children, parents = build_index(
            corpus_dir,
            sample_names,
            size=size,
            overlap=overlap,
            rows_per_parent=12,
            splitter_factory=factory,
            measure=measure,
        )
        children = [c for c in children if c.rel_path in sample_names]
        index = BM25Index(children)

        retriever = Retriever(
            session, workspace.id, settings=settings, embeddings=FakeEmbeddings()
        )
        sample_cases = [c for c in cases if c.file in sample_names][:12]

        mismatches = []
        for case in sample_cases:
            real = retriever.search(
                case.query, top_k=5, use_dense=False, use_rerank=False, use_rewrite=False
            )
            fast = search_bm25(index, children, parents, case.query)
            real_keys = [(r.rel_path, r.location) for r in real]
            fast_keys = [(c.rel_path, c.location) for c, _ in fast]
            if real_keys != fast_keys:
                mismatches.append(
                    {"query": case.query, "real": real_keys[:3], "fast": fast_keys[:3]}
                )
        session.close()
        engine.dispose()
        return {
            "checked": len(sample_cases),
            "mismatches": len(mismatches),
            "detail": mismatches[:3],
        }


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="Chunking parameter sweep")
    parser.add_argument("--sizes", default="256,384,512,768")
    parser.add_argument("--overlaps", default="0,32,64,128")
    parser.add_argument("--rows-per-parent", default="6,12,24")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args()

    sizes = [int(v) for v in args.sizes.split(",")]
    overlaps = [int(v) for v in args.overlaps.split(",")]
    rows_values = [int(v) for v in args.rows_per_parent.split(",")]

    cache_dir = ROOT / "data" / "rag-eval"
    cmrc_path = ensure_cmrc(cache_dir)
    articles = load_cmrc_articles(cmrc_path)

    from app.config import get_settings

    production = get_settings()
    model_path = ""
    if production.embedding_provider == "local" and production.embedding_local_path:
        if Path(production.embedding_local_path).exists():
            model_path = production.embedding_local_path
    print(
        f"measure mode: {'tokens (bge-m3 tokenizer)' if model_path else 'characters (fallback)'}"
    )
    factory, measure = make_measure(model_path)

    with tempfile.TemporaryDirectory(prefix="rag-chunk-eval-") as tmp:
        corpus_dir = Path(tmp) / "corpus"
        corpus_dir.mkdir(parents=True)
        all_cases = build_corpus(corpus_dir)
        cmrc_cases = build_cmrc_corpus(corpus_dir, articles)
        cases = all_cases + cmrc_cases
        print(f"corpus files: {len(list(corpus_dir.iterdir()))}, cases: {len(cases)}")

        case_files = {c.file for c in cases}

        from app.retrieval.bm25 import BM25Index

        # Sub-corpora mirror real workspaces: one index per related file set.
        # Mixing them would let a 60-row sheet out-rank an 8-row sheet's rows
        # for the same person — a competition artifact, not a chunking signal.
        from eval_retrieval import DOC as DEMO_DOC, SHEET as DEMO_SHEET

        subcorpora = [
            (
                "cmrc",
                lambda name: name.startswith(("资料集-", "长文汇编-")),
                lambda case: case.category.startswith("cmrc-"),
            ),
            (
                "biz-demo",
                lambda name: name in {DEMO_DOC, DEMO_SHEET},
                lambda case: case.category in {"biz-doc", "biz-sheet"},
            ),
            (
                "biz-sheet60",
                lambda name: name == "2026年报销明细-大表.xlsx",
                lambda case: case.category == "biz-sheet60",
            ),
        ]

        # ---- grid sweep: chunk_size × overlap ----
        combos = [
            (size, overlap) for size in sizes for overlap in overlaps if overlap < size
        ]
        grid_results = []
        for size, overlap in combos:
            started = time.time()
            children, parents = build_index(
                corpus_dir,
                case_files,
                size=size,
                overlap=overlap,
                rows_per_parent=12,
                splitter_factory=factory,
                measure=measure,
            )
            build_seconds = round(time.time() - started, 2)

            by_subcorpus = {}
            baseline_failures: list[dict] | None = None if (size, overlap) != (512, 64) else []
            for name, file_match, case_match in subcorpora:
                sub_files = {
                    child.rel_path
                    for child in children
                    if file_match(child.rel_path)
                }
                sub_children = [
                    c for c in children if c.rel_path in sub_files
                ]
                sub_cases = [c for c in cases if case_match(c)]
                if not sub_children or not sub_cases:
                    continue
                index = BM25Index(sub_children)
                summary = grade(sub_cases, index, sub_children, parents)
                by_subcorpus[name] = summary
                if baseline_failures is not None:
                    baseline_failures.extend(
                        failed_cases(sub_cases, index, sub_children, parents)
                    )

            stats = structural_stats(children, parents, size=size, measure=measure)
            totals = [
                (v["total"], v["recall@5"], v["mrr@5"])
                for summary in by_subcorpus.values()
                for v in summary.values()
            ]
            case_total = sum(t for t, _, _ in totals)
            recall_all = sum(n * r for n, r, _ in totals) / case_total
            mrr_all = sum(n * m for n, _, m in totals) / case_total
            row = {
                "size": size,
                "overlap": overlap,
                "recall@5": round(recall_all, 4),
                "mrr@5": round(mrr_all, 4),
                "by_subcorpus": by_subcorpus,
                "build_seconds": build_seconds,
                "stats": stats,
            }
            if baseline_failures is not None:
                row["baseline_failures"] = baseline_failures
            grid_results.append(row)
            sub_recall = {
                name: round(
                    sum(v["recall@5"] * v["total"] for v in summary.values())
                    / sum(v["total"] for v in summary.values()),
                    4,
                )
                for name, summary in by_subcorpus.items()
            }
            print(
                f"size={size:<4} overlap={overlap:<4} "
                f"recall@5={recall_all:.4f} mrr@5={mrr_all:.4f} {sub_recall} "
                f"({build_seconds}s, {stats['children']} children, "
                f"ragged={stats['ragged_cut_pct']:.0%})"
            )

        # ---- rows_per_parent sweep on the business sheets ----
        best = max(grid_results, key=lambda r: (r["recall@5"], r["mrr@5"]))
        sheet_cases = [c for c in cases if c.file.endswith(".xlsx")]
        sheet_files = {c.file for c in sheet_cases}
        rows_results = []
        for rows in rows_values:
            children, parents = build_index(
                corpus_dir,
                sheet_files,
                size=best["size"],
                overlap=best["overlap"],
                rows_per_parent=rows,
                splitter_factory=factory,
                measure=measure,
            )
            children = [c for c in children if c.rel_path in sheet_files]
            by_subcorpus = {}
            for name, file_match, case_match in subcorpora[1:]:
                sub_children = [c for c in children if file_match(c.rel_path)]
                sub_cases = [c for c in sheet_cases if case_match(c)]
                if not sub_children or not sub_cases:
                    continue
                index = BM25Index(sub_children)
                by_subcorpus[name] = grade(sub_cases, index, sub_children, parents)
            stats = structural_stats(children, parents, size=best["size"], measure=measure)
            rows_results.append(
                {"rows_per_parent": rows, "by_subcorpus": by_subcorpus, "stats": stats}
            )
            sheet_recall = {
                name: round(
                    sum(v["recall@5"] * v["total"] for v in summary.values())
                    / sum(v["total"] for v in summary.values()),
                    4,
                )
                for name, summary in by_subcorpus.items()
            }
            print(
                f"rows_per_parent={rows:<3} recall@5={sheet_recall} "
                f"parents={stats['parents']} rows/parent p50={stats['excel_rows_per_parent_p50']}"
            )

        # ---- strategy A/B: naive fixed-width chunking (no structure, no parents) ----
        # The comparison everyone asks for: what does the structure-aware
        # parent/child design actually buy over copy-pasted fixed-width splits?
        # Naive children = raw recursive splits of the whole document text; a
        # hit is graded on the chunk itself because there is no parent context.
        from docx import Document as _Docx

        def naive_children(
            path: Path, rel_path: str, size: int, overlap: int, parent_offset: int = 0
        ):
            splitter = factory(size, overlap).splitter
            if path.suffix.lower() == ".docx":
                text = "\n\n".join(
                    p.text for p in _Docx(str(path)).paragraphs if p.text.strip()
                )
            else:
                from openpyxl import load_workbook as _load

                book = _load(path, read_only=True, data_only=True)
                lines = []
                for sheet_name in book.sheetnames:
                    for row in book[sheet_name].iter_rows(values_only=True):
                        if any(v is not None for v in row):
                            lines.append(
                                " | ".join(str(v) for v in row if v is not None)
                            )
                book.close()
                text = "\n".join(lines)
            pieces = splitter.split_text(text)
            rows = []
            for ordinal, piece in enumerate(pieces):
                counts = token_counts(piece)
                rows.append(
                    Child(
                        id=f"naive#{rel_path}.{ordinal}",
                        text=piece,
                        rel_path=rel_path,
                        location=f"块 {ordinal}",
                        parent_index=parent_offset + len(rows),
                        token_counts=counts,
                        token_length=sum(counts.values()),
                    )
                )
            return rows, [Parent(c.text, c.location, rel_path) for c in rows]

        from app.retrieval.bm25 import token_counts

        naive_results = []
        for size, overlap in [(512, 64), (256, 64)]:
            for name, file_match, case_match in subcorpora:
                sub_cases = [c for c in cases if case_match(c)]
                children, parents = [], []
                for path in sorted(corpus_dir.iterdir()):
                    if path.name in case_files and file_match(path.name):
                        parent_offset = len(parents)
                        part_children, part_parents = naive_children(
                            path, path.name, size, overlap, parent_offset
                        )
                        children.extend(part_children)
                        parents.extend(part_parents)
                index = BM25Index(children)
                summary = grade(sub_cases, index, children, parents)
                naive_results.append(
                    {
                        "strategy": "naive-fixed-width",
                        "size": size,
                        "overlap": overlap,
                        "subcorpus": name,
                        "by_category": summary,
                        "children": len(children),
                    }
                )
                sub_recall = round(
                    sum(v["recall@5"] * v["total"] for v in summary.values())
                    / sum(v["total"] for v in summary.values()),
                    4,
                )
                print(
                    f"naive size={size:<4} overlap={overlap:<4} {name:<12} "
                    f"recall@5={sub_recall:.4f} children={len(children)}"
                )

        # ---- strategy A/B: separators extended with the Chinese comma ----
        # A pathological >budget sentence otherwise hard-cuts at ""; giving the
        # splitter "，" before that keeps clause boundaries.
        from transformers import AutoTokenizer as _AT

        from app.retrieval.chunking import BODY_SEPARATORS, BodySplitter

        extended_separators = BODY_SEPARATORS[:-1] + ["，", ",", " ", ""]

        def extended_factory(size: int, overlap: int):
            from langchain_text_splitters import RecursiveCharacterTextSplitter

            splitter = RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
                tokenizer=extended_tokenizer,
                chunk_size=size,
                chunk_overlap=overlap,
                separators=extended_separators,
                keep_separator="end",
            )
            return BodySplitter(splitter=splitter, measure=measure)

        extended_tokenizer = (
            _AT.from_pretrained(model_path) if model_path else None
        )
        separator_results = []
        if extended_tokenizer is not None:
            for label, split_factory in [
                ("baseline-separators", factory),
                ("extended-separators", extended_factory),
            ]:
                children, parents = build_index(
                    corpus_dir,
                    case_files,
                    size=512,
                    overlap=64,
                    rows_per_parent=12,
                    splitter_factory=split_factory,
                    measure=measure,
                )
                stats = structural_stats(
                    children, parents, size=512, measure=measure
                )
                by_subcorpus = {}
                for name, file_match, case_match in subcorpora:
                    sub_children = [
                        c for c in children if file_match(c.rel_path)
                    ]
                    sub_cases = [c for c in cases if case_match(c)]
                    if not sub_children or not sub_cases:
                        continue
                    index = BM25Index(sub_children)
                    by_subcorpus[name] = grade(sub_cases, index, sub_children, parents)
                separator_results.append(
                    {
                        "variant": label,
                        "by_subcorpus": by_subcorpus,
                        "stats": stats,
                    }
                )
                print(
                    f"{label:<22} children={stats['children']} "
                    f"ragged={stats['ragged_cut_pct']:.1%} "
                    f"max={stats['docx_size_max']} "
                    f"cmrc-pressure={by_subcorpus['cmrc']['cmrc-pressure']['recall@5']}"
                )

        equivalence = equivalence_check(
            corpus_dir, cases, size=512, overlap=64, model_path=model_path
        )
        print(
            f"equivalence check: {equivalence['mismatches']}/{equivalence['checked']} mismatches"
        )

        payload = {
            "measure_mode": "tokens" if model_path else "characters",
            "tokenizer": model_path or None,
            "seed": SEED,
            "corpus": {
                "cmrc_articles_standard": STANDARD_FILES * ARTICLES_PER_STANDARD_FILE,
                "cmrc_articles_pressure": PRESSURE_FILES * ARTICLES_PER_PRESSURE_FILE,
                "cmrc_cases_standard": STANDARD_CASES,
                "cmrc_cases_pressure": PRESSURE_FILES * ARTICLES_PER_PRESSURE_FILE,
                "business_cases": len(all_cases),
            },
            "grid": grid_results,
            "best_combo": {"size": best["size"], "overlap": best["overlap"]},
            "rows_sweep": rows_results,
            "naive_baseline": naive_results,
            "separator_variants": separator_results,
            "equivalence": equivalence,
        }
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(f"written: {args.out}")
        else:
            print(
                json.dumps(
                    {k: payload[k] for k in ("best_combo", "equivalence")},
                    ensure_ascii=False,
                )
            )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
