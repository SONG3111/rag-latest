"""Transcript text normalization and conservative noise filtering.

OpenCC (Apache-2.0) is the source of truth for Traditional-to-Simplified
conversion. Whisper's generic ``zh`` language token does not distinguish the
two scripts, so relying on the model prompt alone leaves an observable failure
mode where the same meeting mixes both scripts.
"""

from __future__ import annotations

import re
from functools import lru_cache

_CJK = re.compile(r"[\u3400-\u9fff]")
_NORMALIZE_FOR_MATCH = re.compile(r"[\s，。！？、,.!?：:；;\"'“”‘’（）()]+")

# These are short standalone phrases Whisper commonly emits on music, silence,
# subtitle tracks, or low-volume system audio. Matching is deliberately limited
# to short segments so the same words inside a real sentence remain valid.
_HALLUCINATION_PREFIXES = (
    "字幕by",
    "字幕由",
    "amara.org",
    "www.amara.org",
    "请不吝点赞",
    "明镜与点点",
    "谢谢观看",
)
_PROMPT_LEAKAGE_FRAGMENTS = (
    "以下是简体中文普通话会议，请使用简体中文和规范标点。",
    "以下是简体中文普通话会议",
    "请使用简体中文和规范标点",
    "请使用简体中文普通话会议",
)


@lru_cache(maxsize=1)
def _converter():
    import opencc

    # Whisper's Chinese output is often Taiwan-style Traditional. OpenCC's
    # tw2s profile catches aspect-marker cases such as 躺著 -> 躺着, which the
    # generic t2s profile intentionally leaves unchanged.
    return opencc.OpenCC("tw2s")


def simplify_chinese(text: str) -> str:
    if not text or not _CJK.search(text):
        return text
    return _converter().convert(text)


def normalize_transcript_text(
    text: str,
    *,
    language: str,
    simplify: bool = True,
) -> str:
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    if simplify and (language or "").lower().startswith(("zh", "auto")):
        cleaned = simplify_chinese(cleaned)
    return strip_prompt_leakage(cleaned)


def strip_prompt_leakage(text: str) -> str:
    """Remove the exact ASR instruction text that older versions injected."""
    cleaned = text or ""
    for fragment in _PROMPT_LEAKAGE_FRAGMENTS:
        cleaned = cleaned.replace(fragment, "")
    # Removing the fragment can leave a dangling comma or full-width colon.
    cleaned = re.sub(r"[\s，,、；;：:]+$", "", cleaned)
    return cleaned.strip()


def is_probable_hallucination(
    text: str,
    *,
    start_ms: int,
    end_ms: int,
) -> bool:
    normalized = _NORMALIZE_FOR_MATCH.sub("", (text or "").lower())
    if not normalized:
        return True
    duration_ms = max(0, end_ms - start_ms)
    if duration_ms > 8_000:
        return False
    return normalized.startswith(_HALLUCINATION_PREFIXES)
