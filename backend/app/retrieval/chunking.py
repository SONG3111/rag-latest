"""Document parsing and parent/child chunking.

Chunking serves two competing needs, which is why it produces two levels rather than
one compromise:

* **Recall precision** wants small units. A row-level question ("张伟报销了多少钱")
  is answered by one row, and retrieving a twelve-row block to answer it dilutes the
  signal and makes the citation imprecise.
* **Answer quality** wants context. A row on its own is hard to interpret without the
  header; a clause on its own is hard to place without its section.

So children are indexed and retrieved, and parents are attached afterwards as
context. Parents are never embedded, which also halves embedding cost.

Cutting is structure-aware rather than fixed-width: spreadsheet children are rows
with the header repeated, Word children are paragraphs or table rows. Oversized Word
paragraphs and spreadsheet rows are split by ``langchain-text-splitters`` measured
in *tokens* — the embedding model's own tokenizer, so a chunk's encoded length is
what the vector store actually sees — with Chinese sentence marks leading the
separator list and a character-measured fallback when no local tokenizer is
available.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from docx import Document
from docx.oxml.ns import qn
from openpyxl import load_workbook

logger = logging.getLogger(__name__)

# Word body children are budgeted in tokens; bge-m3 guidance puts retrieval
# units at or below 512 tokens, with ~12% overlap between neighbours.
DEFAULT_CHUNK_TOKENS = 512
DEFAULT_CHUNK_OVERLAP_TOKENS = 64
ROWS_PER_PARENT = 12

# Sentence marks come first so cuts land on clause boundaries; the recursive
# splitter walks down the list only while pieces still exceed the budget.
BODY_SEPARATORS = ["\n\n", "\n", "。", "！", "？", "；", "!", "?", ";", " ", ""]
# Below this the header has eaten nearly the whole budget; count it inside
# instead of reserving, so a pathological section title cannot crowd out content.
MIN_BODY_BUDGET_TOKENS = 64


@dataclass
class TextChunk:
    """A unit of text plus the metadata needed to cite it back to the user."""

    text: str
    location: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class ChunkGroup:
    """One retrievable parent and the precise children underneath it."""

    parent: TextChunk
    children: list[TextChunk]


@dataclass
class BodySplitter:
    """Token-measured splitting of oversized Word paragraphs, header-aware.

    Wraps ``langchain-text-splitters``: pieces are measured with a length
    function (the embedding model's tokenizer when available), never cut
    mid-sentence, and consecutive pieces share an overlap. ``split`` reserves
    the per-child header (file + section line) out of the budget — body pieces
    are cut small enough that the *embedded* child (header + piece) stays
    within ``budget``. A pathologically long header falls back to counting it
    inside the budget rather than crowding the body out entirely.

    Budget contract (pinned by a regression test and discovered the hard way
    in the chunking sweep): ``split`` sizes pieces by its ``budget`` argument,
    never by the splitter's own ``chunk_size`` — ``_resized`` overwrites it.
    Callers injecting a ``body_splitter`` must pass the matching
    ``chunk_size_tokens``/``chunk_overlap_tokens`` arguments too.
    """

    splitter: Any
    measure: Callable[[str], int]

    def split(self, body: str, header: str, budget: int) -> list[str]:
        content_budget = budget - self.measure(header)
        if content_budget < MIN_BODY_BUDGET_TOKENS:
            return self.splitter.split_text(f"{header}{body}")
        piece_splitter = self._resized(content_budget)
        return [f"{header}{piece}" for piece in piece_splitter.split_text(body)]

    def _resized(self, content_budget: int) -> Any:
        """The same strategy with a smaller budget, so header + piece fits.

        ``copy.copy`` keeps the (expensive) tokenizer and only moves the
        limits. A splitter without the langchain attribute layout is used
        as-is, which degrades to the historical header-on-top behaviour.
        """
        resized = copy.copy(self.splitter)
        if getattr(resized, "_chunk_size", None) is None:
            return self.splitter
        resized._chunk_size = max(1, content_budget)
        if getattr(resized, "_chunk_overlap", 0) >= resized._chunk_size:
            resized._chunk_overlap = max(0, resized._chunk_size // 4)
        return resized


@lru_cache(maxsize=2)
def _cached_token_splitter(
    model_path: str, chunk_size: int, chunk_overlap: int
) -> tuple[Any, Callable[[str], int]]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(model_path)
    splitter = RecursiveCharacterTextSplitter.from_huggingface_tokenizer(
        tokenizer=tokenizer,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=BODY_SEPARATORS,
        keep_separator="end",
    )
    return splitter, (lambda text: len(tokenizer.encode(text)))


@lru_cache(maxsize=2)
def _cached_char_splitter(
    chunk_size: int, chunk_overlap: int
) -> tuple[Any, Callable[[str], int]]:
    from langchain_text_splitters import RecursiveCharacterTextSplitter

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        separators=BODY_SEPARATORS,
        keep_separator="end",
        length_function=len,
    )
    return splitter, len


@lru_cache(maxsize=2)
def _resolve_body_splitter(
    model_path: str, chunk_size: int, chunk_overlap: int
) -> tuple[Any, Callable[[str], int]]:
    """Resolve once per (path, budget); remember the fallback after a failure."""
    if model_path:
        try:
            return _cached_token_splitter(model_path, chunk_size, chunk_overlap)
        except Exception as exc:
            logger.warning(
                "no tokenizer at %s (%s); measuring body chunks in characters",
                model_path,
                exc,
            )
    return _cached_char_splitter(chunk_size, chunk_overlap)


def build_body_splitter(
    chunk_size: int, chunk_overlap: int, *, model_path: str | None = None
) -> BodySplitter:
    """Build the Word-body splitter; inject a fake in tests to stay hermetic."""
    if model_path is None:
        from ..config import get_settings

        model_path = get_settings().embedding_local_path
    splitter, measure = _resolve_body_splitter(model_path, chunk_size, chunk_overlap)
    return BodySplitter(splitter=splitter, measure=measure)


def _cell_to_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _column_letter(index: int) -> str:
    letters = ""
    while index > 0:
        index, remainder = divmod(index - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _header_line(header: list[str]) -> str:
    return " | ".join(item for item in header if item)


def _looks_like_header(values: tuple[Any, ...]) -> bool:
    """A conservative header sniff for stacked tables.

    A header row is short text only — numbers, dates, and formulas are data.
    Requiring at least two non-empty cells keeps lone cells from triggering a
    split, so the worst case is a missed split, never a corrupted one.
    """
    cells = [value for value in values if value is not None]
    if len(cells) < 2:
        return False
    return all(
        isinstance(value, str)
        and 0 < len(value.strip()) <= 40
        and not value.lstrip().startswith("=")
        for value in cells
    )


def _row_pairs(header: list[str], row: Iterable[Any]) -> str:
    return ", ".join(
        f"{header[i] or _column_letter(i + 1)}={_cell_to_text(cell)}"
        for i, cell in enumerate(row)
        if _cell_to_text(cell)
    )


# --------------------------------------------------------------------------- #
# Excel
# --------------------------------------------------------------------------- #
def chunk_excel_groups(
    path: Path,
    rel_path: str,
    *,
    rows_per_parent: int = ROWS_PER_PARENT,
    chunk_size_tokens: int = DEFAULT_CHUNK_TOKENS,
    chunk_overlap_tokens: int = DEFAULT_CHUNK_OVERLAP_TOKENS,
    body_splitter: BodySplitter | None = None,
) -> Iterator[ChunkGroup]:
    """Emit one group per row bucket: the bucket is the parent, each row a child.

    Four robustness rules, all learned from adversarial test documents:

    * Formula cells are resolved through a second ``data_only=False`` pass —
      the dual-open pattern ``read_range`` already uses. openpyxl never
      computes values, so a formula written by this application has no cached
      result; without the fallback the whole column would silently vanish
      from the index (and from citations).
    * The header is anchored at the first non-empty row, and row numbers are
      absolute, because sheets whose data starts below row 1 or right of
      column A would otherwise get an empty header and misnumbered rows.
    * Oversized rows go through the same token-measured splitter as Word
      bodies — a character cap let a wide sheet produce 1100-token children,
      double the embedding model's recommended retrieval size.
    * Blank rows separate stacked tables (Excel's current-region semantics),
      and a later block adopts its first row as a header only when that row
      sniffs like one — short text, no numbers — so the second of two stacked
      tables stops being labelled with the first table's columns.
    """
    splitter = body_splitter or build_body_splitter(
        chunk_size_tokens, chunk_overlap_tokens
    )
    values_book = load_workbook(path, read_only=True, data_only=True)
    formula_book = load_workbook(path, read_only=True, data_only=False)
    try:
        for sheet_name in values_book.sheetnames:
            sheet = values_book[sheet_name]

            formula_cells: dict[int, dict[int, str]] = {}
            if sheet_name in formula_book.sheetnames:
                for row_index, row in enumerate(
                    formula_book[sheet_name].iter_rows(values_only=True), start=1
                ):
                    formulas = {
                        column: value
                        for column, value in enumerate(row)
                        if isinstance(value, str) and value.startswith("=")
                    }
                    if formulas:
                        formula_cells[row_index] = formulas

            indexed_rows: list[tuple[int, tuple[Any, ...]]] = []
            for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                if any(value is not None for value in row):
                    indexed_rows.append((row_index, row))
            if not indexed_rows:
                continue

            # Blank rows separate stacked tables — Excel's own current-region
            # semantics. Absolute row numbers jump wherever at least one empty
            # row was skipped, and each jump opens a block that may carry its
            # own header.
            blocks: list[list[tuple[int, tuple[Any, ...]]]] = []
            previous_row_number: int | None = None
            for row_number, row in indexed_rows:
                if previous_row_number is None or row_number > previous_row_number + 1:
                    blocks.append([])
                blocks[-1].append((row_number, row))
                previous_row_number = row_number

            header: list[str] = []
            for block_index, block in enumerate(blocks):
                first_row_number, first_raw = block[0]
                candidate = [
                    _cell_to_text(
                        formula_cells.get(first_row_number, {}).get(column, value)
                    )
                    for column, value in enumerate(first_raw)
                ]
                # The first block keeps the historical rule (first row is the
                # header); later blocks adopt their first row only when it
                # looks like a header, so a real table split by a blank row
                # keeps its original columns.
                header_is_first_row = block_index == 0 or (
                    _looks_like_header(first_raw) and candidate != header
                )
                if header_is_first_row:
                    header = candidate
                preamble = (
                    f"文件：{rel_path}\n工作表：{sheet_name}\n表头：{_header_line(header)}"
                )

                body = block[1:] if header_is_first_row else block
                if not body:
                    row_number = first_row_number
                    text = preamble
                    yield ChunkGroup(
                        parent=TextChunk(
                            text=text,
                            location=f"{sheet_name}!第{row_number}行",
                            meta={"sheet": sheet_name, "row_start": row_number, "row_end": row_number},
                        ),
                        children=[
                            TextChunk(
                                text=text,
                                location=f"{sheet_name}!第{row_number}行",
                                meta={"sheet": sheet_name, "row": row_number},
                            )
                        ],
                    )
                    continue

                for start in range(0, len(body), rows_per_parent):
                    window = body[start : start + rows_per_parent]
                    row_lines: list[tuple[int, str]] = []
                    for row_number, row in window:
                        resolved = tuple(
                            formula_cells.get(row_number, {}).get(column, value)
                            for column, value in enumerate(row)
                        )
                        pairs = _row_pairs(header, resolved)
                        if pairs:
                            row_lines.append((row_number, pairs))
                    if not row_lines:
                        continue

                    first_row = row_lines[0][0]
                    last_row = row_lines[-1][0]
                    parent_text = (
                        f"{preamble}\n"
                        + "\n".join(f"第{number}行: {pairs}" for number, pairs in row_lines)
                    )
                    parent_location = (
                        f"{sheet_name}!第{first_row}行"
                        if first_row == last_row
                        else f"{sheet_name}!第{first_row}-{last_row}行"
                    )

                    children: list[TextChunk] = []
                    for number, pairs in row_lines:
                        row_line = f"第{number}行: {pairs}"
                        pieces = splitter.split(row_line, f"{preamble}\n", chunk_size_tokens)
                        for piece_index, piece in enumerate(pieces):
                            children.append(
                                TextChunk(
                                    text=piece,
                                    location=f"{sheet_name}!第{number}行",
                                    meta={
                                        "sheet": sheet_name,
                                        "row": number,
                                        "part": piece_index,
                                    },
                                )
                            )

                    yield ChunkGroup(
                        parent=TextChunk(
                            text=parent_text,
                            location=parent_location,
                            meta={
                                "sheet": sheet_name,
                                "row_start": first_row,
                                "row_end": last_row,
                            },
                        ),
                        children=children,
                    )
    finally:
        values_book.close()
        formula_book.close()


# --------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------- #
def _ppr_has_heading_outline(ppr: Any) -> bool:
    """True when a ``w:pPr`` declares an outline level 0-8.

    OOXML reserves 0-8 for heading levels 1-9; 9 is the body-text sentinel.
    """
    if ppr is None:
        return False
    level = ppr.find(qn("w:outlineLvl"))
    if level is None:
        return False
    value = level.get(qn("w:val"))
    return bool(value and value.isdigit() and int(value) <= 8)


def _is_heading(paragraph: Any) -> bool:
    """Whether a Word paragraph opens a new section.

    Ported from docling's ``msword_backend._get_label_and_level`` (MIT):
    a paragraph is a heading when any style on its inheritance chain has
    "heading" in its name or id, or carries an OOXML ``w:outlineLvl`` of
    0-8. The outline level is language-independent, so localized and custom
    heading styles are caught even when their name never spells "Heading".
    Direct paragraph formatting (``w:pPr/w:outlineLvl``) counts too.
    """
    style = paragraph.style
    visited: set[str] = set()
    while style is not None and style.style_id not in visited:
        visited.add(style.style_id)
        name = (style.name or "").lower()
        style_id = (style.style_id or "").lower()
        if "heading" in name or "heading" in style_id:
            return True
        if _ppr_has_heading_outline(style.element.find(qn("w:pPr"))):
            return True
        style = style.base_style
    return _ppr_has_heading_outline(paragraph._p.pPr)


def chunk_word_groups(
    path: Path,
    rel_path: str,
    *,
    chunk_size_tokens: int = DEFAULT_CHUNK_TOKENS,
    chunk_overlap_tokens: int = DEFAULT_CHUNK_OVERLAP_TOKENS,
    body_splitter: BodySplitter | None = None,
) -> Iterator[ChunkGroup]:
    """Emit one group per heading section: the section is the parent, paragraphs children."""
    document = Document(str(path))
    paragraphs = list(document.paragraphs)
    splitter = body_splitter or build_body_splitter(chunk_size_tokens, chunk_overlap_tokens)

    section_title = ""
    section_lines: list[tuple[int, str]] = []

    def build_group(title: str, lines: list[tuple[int, str]]) -> ChunkGroup | None:
        if not lines:
            return None
        header = f"文件：{rel_path}\n" + (f"章节：{title}\n" if title else "")
        body = "\n".join(text for _, text in lines if text.strip())
        if not body.strip():
            return None

        first_index = lines[0][0]
        last_index = lines[-1][0]

        children: list[TextChunk] = []
        for index, text in lines:
            # The header is prepended after splitting so it never eats the
            # content budget; a paragraph that fits stays a single child.
            for piece_index, piece in enumerate(splitter.split(text, header, chunk_size_tokens)):
                children.append(
                    TextChunk(
                        text=piece,
                        location=f"段落 {index}",
                        meta={
                            "heading": title,
                            "paragraph": index,
                            "part": piece_index,
                        },
                    )
                )
        if not children:
            return None

        return ChunkGroup(
            parent=TextChunk(
                text=header + body,
                location=(
                    f"段落 {first_index}"
                    if first_index == last_index
                    else f"段落 {first_index}-{last_index}"
                ),
                meta={
                    "heading": title,
                    "paragraph_start": first_index,
                    "paragraph_end": last_index,
                },
            ),
            children=children,
        )

    for index, paragraph in enumerate(paragraphs):
        text = paragraph.text.strip()
        if _is_heading(paragraph):
            group = build_group(section_title, section_lines)
            if group is not None:
                yield group
            section_title = text
            section_lines = []
            continue
        if text:
            section_lines.append((index, text))

    group = build_group(section_title, section_lines)
    if group is not None:
        yield group

    # Tables carry much of the answerable content in policy documents, so each table
    # becomes its own group and each row its own retrievable child.
    for table_index, table in enumerate(document.tables):
        header = f"文件：{rel_path}\n表格 {table_index}：\n"
        rendered: list[str] = []
        children: list[TextChunk] = []
        for row_index, row in enumerate(table.rows):
            cells = [cell.text.strip() for cell in row.cells]
            if not any(cells):
                continue
            line = " | ".join(cells)
            rendered.append(line)
            children.append(
                TextChunk(
                    text=f"{header}{line}",
                    location=f"表格 {table_index} 第 {row_index + 1} 行",
                    meta={"table_index": table_index, "row": row_index},
                )
            )
        if not rendered:
            continue
        yield ChunkGroup(
            parent=TextChunk(
                text=header + "\n".join(rendered),
                location=f"表格 {table_index}",
                meta={"table_index": table_index},
            ),
            children=children,
        )


# --------------------------------------------------------------------------- #
# dispatch
# --------------------------------------------------------------------------- #
def chunk_document_groups(
    path: Path,
    rel_path: str,
    *,
    chunk_size_tokens: int = DEFAULT_CHUNK_TOKENS,
    chunk_overlap_tokens: int = DEFAULT_CHUNK_OVERLAP_TOKENS,
    body_splitter: BodySplitter | None = None,
) -> list[ChunkGroup]:
    """Dispatch to the parser matching the file type."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return list(
            chunk_excel_groups(
                path,
                rel_path,
                chunk_size_tokens=chunk_size_tokens,
                chunk_overlap_tokens=chunk_overlap_tokens,
                body_splitter=body_splitter,
            )
        )
    if suffix == ".docx":
        return list(
            chunk_word_groups(
                path,
                rel_path,
                chunk_size_tokens=chunk_size_tokens,
                chunk_overlap_tokens=chunk_overlap_tokens,
                body_splitter=body_splitter,
            )
        )
    return []


def chunk_document(
    path: Path,
    rel_path: str,
    *,
    chunk_size_tokens: int = DEFAULT_CHUNK_TOKENS,
    chunk_overlap_tokens: int = DEFAULT_CHUNK_OVERLAP_TOKENS,
    body_splitter: BodySplitter | None = None,
) -> list[TextChunk]:
    """Flatten a document into retrievable children.

    Kept for callers that only need the searchable units; the indexing path uses
    :func:`chunk_document_groups` because it also has to persist the parents.
    """
    return [
        child
        for group in chunk_document_groups(
            path,
            rel_path,
            chunk_size_tokens=chunk_size_tokens,
            chunk_overlap_tokens=chunk_overlap_tokens,
            body_splitter=body_splitter,
        )
        for child in group.children
    ]


__all__ = [
    "BodySplitter",
    "ChunkGroup",
    "TextChunk",
    "build_body_splitter",
    "chunk_document",
    "chunk_document_groups",
    "chunk_excel_groups",
    "chunk_word_groups",
]
