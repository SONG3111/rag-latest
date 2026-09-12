"""Word implementation layer built on python-docx.

The interesting problem here is text replacement. Word splits a logical sentence
across multiple ``run`` objects whenever formatting changes mid-sentence, so a
naive per-run ``str.replace`` silently fails on any text the user actually cares
about. We instead rebuild each paragraph's text run-by-run, so a match spanning
several runs is still found. Formatting is preserved by keeping the first run's
style and moving the remaining text into it only when a replacement actually
crosses run boundaries.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from docx import Document
from docx.document import Document as DocxDocument
from docx.oxml.text.paragraph import CT_P
from docx.text.paragraph import Paragraph

from .errors import (
    FileLocked,
    CorruptDocument,
    ParagraphNotFound,
    RangeError,
    TableNotFound,
    ToolError,
)

HEADING_STYLE_PREFIX = "Heading"
MAX_PARAGRAPH_CHARS = 4000


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def open_document(path: Path) -> DocxDocument:
    try:
        return Document(str(path))
    except Exception as exc:
        raise CorruptDocument(f"cannot open document '{path.name}': {exc}") from exc


def _iter_body_paragraphs(document: DocxDocument) -> list[Paragraph]:
    """Return top-level body paragraphs in document order."""
    return list(document.paragraphs)


def _heading_level(paragraph: Paragraph) -> int | None:
    style_name = (paragraph.style.name or "") if paragraph.style is not None else ""
    if not style_name.startswith(HEADING_STYLE_PREFIX):
        return None
    suffix = style_name[len(HEADING_STYLE_PREFIX) :].strip()
    if not suffix:
        return 1
    try:
        return int(suffix)
    except ValueError:
        return 1


def _paragraph_text(paragraph: Paragraph) -> str:
    return "".join(run.text for run in paragraph.runs)


def _table_to_rows(table) -> list[list[str]]:
    return [[cell.text for cell in row.cells] for row in table.rows]


# --------------------------------------------------------------------------- #
# read operations
# --------------------------------------------------------------------------- #
def document_structure(path: Path) -> dict:
    """Outline the document: heading tree, paragraph count, and table index."""
    document = open_document(path)
    paragraphs = _iter_body_paragraphs(document)

    outline: list[dict] = []
    for index, paragraph in enumerate(paragraphs):
        level = _heading_level(paragraph)
        text = _paragraph_text(paragraph).strip()
        if level is not None and text:
            outline.append({"paragraph_index": index, "level": level, "text": text})

    tables = [
        {
            "table_index": index,
            "rows": len(table.rows),
            "columns": len(table.columns),
            "header": _table_to_rows(table)[0] if len(table.rows) else [],
        }
        for index, table in enumerate(document.tables)
    ]

    return {
        "kind": "word",
        "paragraph_count": len(paragraphs),
        "table_count": len(tables),
        "outline": outline,
        "tables": tables,
    }


def read_paragraphs(
    path: Path,
    *,
    start: int = 0,
    end: int | None = None,
    max_paragraphs: int = 200,
    max_chars: int = MAX_PARAGRAPH_CHARS,
) -> dict:
    """Read a slice of body paragraphs by index."""
    document = open_document(path)
    paragraphs = _iter_body_paragraphs(document)
    total = len(paragraphs)

    if start < 0:
        raise RangeError("start index must be >= 0")
    if start >= total and total > 0:
        raise ParagraphNotFound(
            f"start index {start} is beyond the document ({total} paragraphs)"
        )

    stop = total if end is None else min(end + 1, total)
    stop = min(stop, start + max_paragraphs)

    items: list[dict] = []
    used_chars = 0
    for index in range(start, stop):
        paragraph = paragraphs[index]
        text = _paragraph_text(paragraph)
        if used_chars + len(text) > max_chars and items:
            break
        used_chars += len(text)
        items.append(
            {
                "index": index,
                "text": text,
                "style": paragraph.style.name if paragraph.style is not None else None,
                "is_heading": _heading_level(paragraph) is not None,
            }
        )

    return {
        "kind": "word",
        "total_paragraphs": total,
        "start": start,
        "returned": len(items),
        "paragraphs": items,
    }


def read_table(path: Path, table_index: int, *, max_rows: int = 100) -> dict:
    document = open_document(path)
    if table_index < 0 or table_index >= len(document.tables):
        raise TableNotFound(
            f"table index {table_index} not found; document has {len(document.tables)} tables"
        )
    table = document.tables[table_index]
    rows = _table_to_rows(table)
    truncated = len(rows) > max_rows
    return {
        "kind": "word",
        "table_index": table_index,
        "rows": rows[:max_rows],
        "columns": len(table.columns),
        "truncated": truncated,
    }


def find_text(path: Path, query: str, *, max_hits: int = 50) -> list[dict]:
    """Case-insensitive search across paragraphs and table cells."""
    needle = query.casefold()
    hits: list[dict] = []
    document = open_document(path)

    for index, paragraph in enumerate(_iter_body_paragraphs(document)):
        text = _paragraph_text(paragraph)
        if needle in text.casefold():
            hits.append({"location": "paragraph", "paragraph_index": index, "text": text})
            if len(hits) >= max_hits:
                return hits

    for table_index, table in enumerate(document.tables):
        for row_index, row in enumerate(table.rows):
            for col_index, cell in enumerate(row.cells):
                text = cell.text
                if needle in text.casefold():
                    hits.append(
                        {
                            "location": "table_cell",
                            "table_index": table_index,
                            "row": row_index,
                            "column": col_index,
                            "text": text,
                        }
                    )
                    if len(hits) >= max_hits:
                        return hits
    return hits


# --------------------------------------------------------------------------- #
# write operations
# --------------------------------------------------------------------------- #
def _replace_in_paragraph(paragraph: Paragraph, find: str, replace: str, count: int) -> int:
    """Replace text inside one paragraph, tolerating matches that span runs.

    Returns the number of replacements performed.
    """
    runs = paragraph.runs
    if not runs:
        return 0

    combined = "".join(run.text for run in runs)
    if find not in combined:
        return 0

    replaced_count = combined.count(find) if count <= 0 else min(combined.count(find), count)
    if replaced_count == 0:
        return 0

    updated = combined.replace(find, replace, count if count > 0 else -1)

    # Collapse the paragraph into its first run so replacement never leaves the
    # original fragments behind, then re-apply the leading run's formatting.
    runs[0].text = updated
    for run in runs[1:]:
        run.text = ""
    return replaced_count


def save_document(document, path: Path) -> None:
    """Persist the document, translating OS-level write failures into ToolError."""
    try:
        document.save(str(path))
    except PermissionError as exc:
        raise FileLocked(
            "文件正被其他程序占用（如 Word/WPS），请关闭后重试",
            detail=str(exc),
        ) from exc
    except OSError as exc:
        raise ToolError(f"写入文件失败: {exc}", detail=str(exc)) from exc


def replace_text(
    path: Path,
    find: str,
    replace: str,
    *,
    count: int = 0,
    include_tables: bool = True,
) -> dict:
    """Find and replace text throughout the document.

    Args:
        count: Maximum replacements per paragraph; ``0`` means replace all.
    """
    if not find:
        raise RangeError("'find' must not be empty")

    document = open_document(path)
    changes: list[dict] = []
    total = 0

    for index, paragraph in enumerate(_iter_body_paragraphs(document)):
        before = _paragraph_text(paragraph)
        hits = _replace_in_paragraph(paragraph, find, replace, count)
        if hits:
            total += hits
            changes.append(
                {
                    "location": "paragraph",
                    "paragraph_index": index,
                    "before": before,
                    "after": _paragraph_text(paragraph),
                    "occurrences": hits,
                }
            )

    if include_tables:
        for table_index, table in enumerate(document.tables):
            for row_index, row in enumerate(table.rows):
                for col_index, cell in enumerate(row.cells):
                    for paragraph in cell.paragraphs:
                        before = _paragraph_text(paragraph)
                        hits = _replace_in_paragraph(paragraph, find, replace, count)
                        if hits:
                            total += hits
                            changes.append(
                                {
                                    "location": "table_cell",
                                    "table_index": table_index,
                                    "row": row_index,
                                    "column": col_index,
                                    "before": before,
                                    "after": _paragraph_text(paragraph),
                                    "occurrences": hits,
                                }
                            )

    if total:
        save_document(document, path)

    return {
        "kind": "word",
        "path": path.name,
        "total_replacements": total,
        "changes": changes,
    }


def update_table_cell(
    path: Path,
    table_index: int,
    row: int,
    column: int,
    value: str,
) -> dict:
    """Overwrite the text of a table cell, keeping the first run's formatting."""
    document = open_document(path)
    if table_index < 0 or table_index >= len(document.tables):
        raise TableNotFound(
            f"table index {table_index} not found; document has {len(document.tables)} tables"
        )

    table = document.tables[table_index]
    if row < 0 or row >= len(table.rows):
        raise RangeError(f"row {row} is out of range (table has {len(table.rows)} rows)")
    if column < 0 or column >= len(table.columns):
        raise RangeError(
            f"column {column} is out of range (table has {len(table.columns)} columns)"
        )

    cell = table.cell(row, column)
    before = cell.text
    if cell.paragraphs:
        paragraph = cell.paragraphs[0]
        if paragraph.runs:
            paragraph.runs[0].text = value
            for run in paragraph.runs[1:]:
                run.text = ""
        else:
            paragraph.add_run(value)
        # Drop any additional paragraphs so the cell content is deterministic.
        for extra in cell.paragraphs[1:]:
            extra._element.getparent().remove(extra._element)
    else:  # pragma: no cover - python-docx always creates one paragraph
        cell.add_paragraph(value)

    save_document(document, path)
    return {
        "kind": "word",
        "changes": [
            {
                "location": "table_cell",
                "table_index": table_index,
                "row": row,
                "column": column,
                "before": before,
                "after": value,
            }
        ],
    }


__all__ = [
    "document_structure",
    "find_text",
    "open_document",
    "read_paragraphs",
    "read_table",
    "replace_text",
    "update_table_cell",
]
