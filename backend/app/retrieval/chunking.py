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
with the header repeated, Word children are paragraphs or table rows, and splitting
respects Chinese punctuation because sentence boundaries differ from the Latin
convention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from docx import Document
from openpyxl import load_workbook

CHINESE_SENTENCE_END = "。！？；!?;\n"
DEFAULT_CHUNK_SIZE = 700
DEFAULT_OVERLAP = 80
ROWS_PER_PARENT = 12
MAX_CHILD_CHARS = 1200


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


_SENTENCE_PATTERN = (
    rf"[^{re.escape(CHINESE_SENTENCE_END)}]*"
    rf"[{re.escape(CHINESE_SENTENCE_END)}]?"
)


def _split_long_text(text: str, size: int, overlap: int) -> list[str]:
    """Split oversized text on sentence boundaries, with a small overlap."""
    if len(text) <= size:
        return [text] if text.strip() else []

    sentences = re.findall(_SENTENCE_PATTERN, text)
    pieces: list[str] = []
    buffer = ""
    for sentence in sentences:
        if not sentence:
            continue
        if len(buffer) + len(sentence) <= size:
            buffer += sentence
            continue
        if buffer.strip():
            pieces.append(buffer)
        tail = buffer[-overlap:] if overlap and buffer else ""
        buffer = tail + sentence
        while len(buffer) > size:
            pieces.append(buffer[:size])
            buffer = buffer[size - overlap :] if overlap else buffer[size:]
    if buffer.strip():
        pieces.append(buffer)
    return pieces


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


def _row_pairs(header: list[str], row: tuple) -> str:
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
) -> Iterator[ChunkGroup]:
    """Emit one group per row bucket: the bucket is the parent, each row a child."""
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        for sheet_name in workbook.sheetnames:
            sheet = workbook[sheet_name]
            rows = list(sheet.iter_rows(values_only=True))
            if not rows:
                continue

            header = [_cell_to_text(value) for value in rows[0]]
            header_label = _header_line(header)
            body = rows[1:] if len(rows) > 1 else []
            preamble = f"文件：{rel_path}\n工作表：{sheet_name}\n表头：{header_label}"

            if not body:
                text = preamble
                yield ChunkGroup(
                    parent=TextChunk(
                        text=text,
                        location=f"{sheet_name}!第1行",
                        meta={"sheet": sheet_name, "row_start": 1, "row_end": 1},
                    ),
                    children=[
                        TextChunk(
                            text=text,
                            location=f"{sheet_name}!第1行",
                            meta={"sheet": sheet_name, "row": 1},
                        )
                    ],
                )
                continue

            for start in range(0, len(body), rows_per_parent):
                window = body[start : start + rows_per_parent]
                row_lines: list[tuple[int, str]] = []
                for offset, row in enumerate(window):
                    row_number = start + offset + 2
                    pairs = _row_pairs(header, row)
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
                    child_text = f"{preamble}\n第{number}行: {pairs}"
                    for piece_index, piece in enumerate(
                        _split_long_text(child_text, MAX_CHILD_CHARS, 0)
                    ):
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
        workbook.close()


# --------------------------------------------------------------------------- #
# Word
# --------------------------------------------------------------------------- #
def chunk_word_groups(
    path: Path,
    rel_path: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> Iterator[ChunkGroup]:
    """Emit one group per heading section: the section is the parent, paragraphs children."""
    document = Document(str(path))
    paragraphs = list(document.paragraphs)

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
            child_text = f"{header}{text}"
            for piece_index, piece in enumerate(
                _split_long_text(child_text, chunk_size, 0)
            ):
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
        style_name = paragraph.style.name if paragraph.style is not None else ""
        is_heading = bool(style_name) and style_name.startswith("Heading")
        if is_heading:
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
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list[ChunkGroup]:
    """Dispatch to the parser matching the file type."""
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xlsm"}:
        return list(chunk_excel_groups(path, rel_path))
    if suffix == ".docx":
        return list(chunk_word_groups(path, rel_path, chunk_size=chunk_size))
    return []


def chunk_document(
    path: Path,
    rel_path: str,
    *,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> list[TextChunk]:
    """Flatten a document into retrievable children.

    Kept for callers that only need the searchable units; the indexing path uses
    :func:`chunk_document_groups` because it also has to persist the parents.
    """
    return [
        child
        for group in chunk_document_groups(path, rel_path, chunk_size=chunk_size)
        for child in group.children
    ]


__all__ = [
    "ChunkGroup",
    "TextChunk",
    "chunk_document",
    "chunk_document_groups",
    "chunk_excel_groups",
    "chunk_word_groups",
]
