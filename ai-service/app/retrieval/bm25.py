"""Okapi BM25 over the chunk table.

The keyword leg of hybrid retrieval is implemented in-process rather than delegated
to Elasticsearch or SQLite's FTS5, because Chinese text needs tokenization that FTS5
does not provide out of the box and because it keeps the whole application runnable
with no external service. Token counts are persisted per chunk at index time, so a
query only walks the postings it needs.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter, defaultdict
from typing import TYPE_CHECKING, Iterable

import jieba

if TYPE_CHECKING:
    from .pipeline import ChunkRecord

K1 = 1.5
B = 0.75

_LATIN_WORD = re.compile(r"[A-Za-z0-9_]+")
_CJK = re.compile(r"[\u4e00-\u9fff]")

# jieba is noisy on stderr the first time it builds its dictionary.
jieba.setLogLevel(60)


def tokenize(text: str) -> list[str]:
    """Tokenize mixed Chinese/English text into BM25 terms."""
    if not text:
        return []
    lowered = text.lower()
    tokens: list[str] = []
    for word in jieba.cut(lowered):
        word = word.strip()
        if not word:
            continue
        if _CJK.search(word):
            # A segmented token may still be multi-character; index both the whole
            # token and its bigrams so partial matches ("销售额" vs "销售") still hit.
            tokens.append(word)
            if len(word) > 2:
                tokens.extend(word[i : i + 2] for i in range(len(word) - 1))
        elif _LATIN_WORD.fullmatch(word):
            tokens.append(word)
    return tokens


def token_counts(text: str) -> dict[str, int]:
    return dict(Counter(tokenize(text)))


class BM25Index:
    """Snapshot of a workspace's chunks, scored in memory."""

    def __init__(self, chunks: Iterable[ChunkRecord]) -> None:
        self.chunk_ids: list[str] = []
        self.token_counts: list[dict[str, int]] = []
        self.lengths: list[int] = []
        self.postings: dict[str, list[int]] = defaultdict(list)
        self._df: Counter[str] = Counter()

        for chunk in chunks:
            counts = chunk.token_counts or {}
            if not isinstance(counts, dict):
                counts = {}
            index = len(self.chunk_ids)
            self.chunk_ids.append(chunk.id)
            self.token_counts.append(counts)
            length = int(chunk.token_length or sum(counts.values()))
            self.lengths.append(length)
            for token in counts:
                self.postings[token].append(index)
                self._df[token] += 1

        self.total_docs = len(self.chunk_ids)
        self.avg_length = (
            sum(self.lengths) / self.total_docs if self.total_docs else 0.0
        )

    def _idf(self, token: str) -> float:
        df = self._df.get(token, 0)
        if df == 0:
            return 0.0
        return math.log(1 + (self.total_docs - df + 0.5) / (df + 0.5))

    def search(self, query: str, top_k: int = 20) -> list[tuple[str, float]]:
        """Return ``(chunk_id, score)`` pairs, best first."""
        if not self.total_docs:
            return []

        query_tokens = tokenize(query)
        if not query_tokens:
            return []

        scores: dict[int, float] = defaultdict(float)
        for token in set(query_tokens):
            idf = self._idf(token)
            if idf <= 0:
                continue
            for index in self.postings.get(token, ()):
                counts = self.token_counts[index]
                freq = counts.get(token, 0)
                if not freq:
                    continue
                length = self.lengths[index] or 1
                denominator = freq + K1 * (
                    1 - B + B * (length / (self.avg_length or 1.0))
                )
                scores[index] += idf * (freq * (K1 + 1)) / denominator

        if not scores:
            return []

        ranked = sorted(scores.items(), key=lambda item: -item[1])[:top_k]
        return [(self.chunk_ids[index], score) for index, score in ranked]


def reciprocal_rank_fusion(
    ranked_lists: list[list[str]], *, k: int = 60, weights: list[float] | None = None
) -> list[tuple[str, float]]:
    """Fuse multiple ranked id lists into one ranking.

    RRF is used instead of score normalization because BM25 scores and cosine
    distances live on incompatible scales; only the ordering is comparable.
    """
    weights = weights or [1.0] * len(ranked_lists)
    fused: dict[str, float] = defaultdict(float)
    for weight, ranked in zip(weights, ranked_lists):
        for rank, item_id in enumerate(ranked):
            fused[item_id] += weight / (k + rank + 1)
    return sorted(fused.items(), key=lambda item: -item[1])


_FINGERPRINT_CHARS = 200
_WHITESPACE = re.compile(r"\s+")


def content_fingerprint(text: str, *, chars: int = _FINGERPRINT_CHARS) -> str:
    """A stable identity for a passage, used to collapse duplicate recall.

    The same passage is routinely returned by both the dense and the sparse leg —
    that is expected, not a bug — but it must occupy one slot rather than two. The
    fingerprint is deliberately a prefix hash rather than a full-text hash: chunks
    that differ only in trailing context are the same passage for ranking purposes.

    Normalization happens before truncation, otherwise two renderings of the same
    passage that differ only in whitespace would be truncated at different offsets
    and hash differently.
    """
    normalized = _WHITESPACE.sub("", text or "")[:chars]
    return hashlib.sha1(normalized.encode("utf-8")).hexdigest()
