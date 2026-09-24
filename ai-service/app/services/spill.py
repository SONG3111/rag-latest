"""Oversized tool-result spilling: preview + retrieval guidance in context.

Pattern from DeepSeek's harness (deepseek-ai/deepseek-harness,
``packages/spill/README.md``): oversized text is stored outside the context
window and the model receives "a small locator with retrieval guidance"
instead of the bulky content. Two adaptations for this single-machine stack,
both verified against the code rather than assumed:

* the store is ``data_dir/spill/<workspace_id>/`` — outside the workspace tree,
  so a spill file can never enter the vector index. (Indexing is per uploaded
  file record with no directory scan, but keeping the store outside the
  workspace makes that invariant hold by construction, not by convention.)
* there is no plain-text read tool to hand the locator to — the MCP read tools
  are office-only — so the guidance points at re-calling the same tool with
  narrower arguments (smaller range, more specific query), not at reading the
  spill file. The file itself is kept for observability, not for the model.

Failure keeps the original result (dsh: "keeps the original result on storage
failure") — spilling is an optimization, never a data-loss risk.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

from ..config import Settings

logger = logging.getLogger(__name__)

# Same figure pi-mono uses when serializing tool results for summarization
# (docs/compaction.md): enough to keep a result's head — headers, first rows,
# score structure — without renting most of the context window.
_PREVIEW_CHARS = 2000

_NOTE = (
    "结果过长已截断（完整结果已保存到服务端 {file}）。"
    "如需其余内容，请用更窄的参数重新调用该工具：更具体/更局部的检索问题、"
    "更小的行列范围或更少的段落数。"
)


def spill_tool_result(
    tool: str, content: str, *, workspace_id: str, settings: Settings
) -> str | None:
    """The context-safe replacement for an oversized tool result, or None.

    None means "keep as-is": the result is under ``tool_result_spill_chars``,
    or the spill write failed (the caller then sends the full text, exactly as
    before this mechanism existed).
    """
    if len(content) <= settings.tool_result_spill_chars:
        return None
    preview = content[:_PREVIEW_CHARS]
    try:
        directory = Path(settings.data_dir) / "spill" / workspace_id
        directory.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in tool) or "tool"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        path = directory / f"{safe}-{stamp}.txt"
        path.write_text(content, encoding="utf-8")
        locator = f"spill/{workspace_id}/{path.name}"
    except OSError as exc:
        logger.warning("spill write failed for tool %s; keeping the full result: %s", tool, exc)
        return None

    return json.dumps(
        {
            "ok": True,
            "data": {
                "status": "spilled",
                "tool": tool,
                "preview": preview,
                "note": _NOTE.format(file=locator),
                "spill_file": locator,
            },
        },
        ensure_ascii=False,
    )
