"""Factories for chat, embedding, and reranking models.

The three roles are deliberately independent, because they have different cost and
privacy profiles:

* **Chat** runs on Aliyun Model Studio (百炼) through its OpenAI-compatible endpoint,
  built with ``langchain-openai`` and a custom ``base_url``. Nothing about the code
  is Bailian-specific beyond configuration.
* **Embedding** can run either against the same cloud endpoint or entirely locally
  from downloaded sentence-transformers weights. Indexing a private corpus does not
  need to leave the machine, so the local path is a first-class option rather than a
  fallback.
* **Reranking** has no binding in the LangChain OpenAI package, so the DashScope
  native endpoint is called over HTTP behind a small interface, with a local
  cross-encoder as the offline alternative and a no-op that degrades to plain RRF.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from functools import lru_cache
from pathlib import Path

import httpx
from langchain_core.embeddings import Embeddings
from langchain_openai import ChatOpenAI, OpenAIEmbeddings
from openai import DefaultAsyncHttpxClient, DefaultHttpxClient

from ..config import Settings, get_settings

logger = logging.getLogger(__name__)

_DASHSCOPE_RERANK_PATH = "/api/v1/services/rerank/text-rerank/text-rerank"


def _direct_http_clients(timeout: float):
    """Build sync and async httpx clients that ignore every proxy setting.

    This is not a hardening nicety, it fixes a real and very confusing failure.
    httpx reads proxy configuration from the environment *and*, on Windows, from the
    system registry. So a process started while a system proxy is enabled keeps
    dialing that proxy for its whole lifetime: if the user later turns the proxy off,
    every model call dies with ``WinError 10061`` (connection refused) even though
    the proxy is no longer configured and a fresh request from any other tool
    succeeds. Diagnosing that from the outside is painful because the symptom points
    at the model vendor rather than the local network configuration.

    This application only talks to a cloud model endpoint and a local MCP subprocess,
    so honouring a proxy can only ever break it.

    The classes derive from ``openai.DefaultHttpxClient`` because the OpenAI SDK
    checks the client type rather than duck-typing it.
    """
    limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)

    class _DirectSyncClient(DefaultHttpxClient):
        def __init__(self) -> None:
            super().__init__(timeout=timeout, limits=limits, trust_env=False)

    class _DirectAsyncClient(DefaultAsyncHttpxClient):
        def __init__(self) -> None:
            super().__init__(timeout=timeout, limits=limits, trust_env=False)

    return _DirectSyncClient(), _DirectAsyncClient()


class ProviderError(RuntimeError):
    """Raised when a provider call fails irrecoverably."""


def _require_api_key(settings: Settings) -> str:
    if not settings.dashscope_api_key:
        raise ProviderError(
            "DASHSCOPE_API_KEY is not set. Copy .env.example to .env and fill it in."
        )
    return settings.dashscope_api_key


@lru_cache(maxsize=4)
def _cached_llm(
    api_key: str,
    base_url: str,
    model: str,
    temperature: float,
    timeout: float,
    enable_thinking: bool,
) -> ChatOpenAI:
    http_client, http_async_client = _direct_http_clients(timeout)
    return ChatOpenAI(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=temperature,
        timeout=timeout,
        max_retries=3,
        # DashScope reads this from the request body. Reasoning models otherwise spend
        # ~8s producing reasoning tokens before the first visible answer token, which
        # reads as a hang in a streaming UI.
        extra_body={"enable_thinking": enable_thinking},
        http_client=http_client,
        http_async_client=http_async_client,
    )


def build_chat_model(
    settings: Settings | None = None,
    *,
    model: str | None = None,
    temperature: float | None = None,
    timeout: float | None = None,
) -> ChatOpenAI:
    """Return the chat model used by the agent."""
    settings = settings or get_settings()
    return _cached_llm(
        _require_api_key(settings),
        settings.llm_base_url,
        model or settings.llm_model,
        settings.llm_temperature if temperature is None else temperature,
        settings.llm_request_timeout if timeout is None else timeout,
        settings.llm_enable_thinking,
    )


@lru_cache(maxsize=4)
def _cached_embeddings(
    api_key: str, base_url: str, model: str, dimensions: int
) -> OpenAIEmbeddings:
    # Model Studio rejects batched embedding requests above this size.
    return OpenAIEmbeddings(
        api_key=api_key,
        base_url=base_url,
        model=model,
        dimensions=dimensions,
        chunk_size=10,
        check_embedding_ctx_length=False,
    )


def build_embeddings(settings: Settings | None = None) -> Embeddings:
    """Return the embedding model used for indexing and retrieval."""
    settings = settings or get_settings()
    provider = (settings.embedding_provider or "dashscope").strip().lower()

    if provider in {"local", "local-bge", "bge", "sentence-transformers", "st"}:
        return _build_local_embeddings(settings)

    return _cached_embeddings(
        _require_api_key(settings),
        settings.effective_embedding_base_url,
        settings.embedding_model,
        settings.embedding_dimensions,
    )


def _local_model_reference(configured_path: str, fallback_name: str) -> tuple[str, bool]:
    """Resolve a local model reference.

    Returns the path/name to hand to the loader and whether the weights are expected
    to be available offline. A directory that exists is treated as the source of
    truth; otherwise the name is passed through and the hub cache is consulted.
    """
    candidate = (configured_path or "").strip()
    if not candidate:
        return fallback_name, False
    expanded = Path(os.path.expandvars(os.path.expanduser(candidate)))
    if expanded.exists():
        return str(expanded), True
    logger.warning(
        "configured local model path does not exist, falling back to model name '%s': %s",
        fallback_name,
        candidate,
    )
    return fallback_name, False


@lru_cache(maxsize=2)
def _cached_local_embeddings(
    model_ref: str,
    device: str,
    batch_size: int,
    normalize: bool,
    offline: bool,
):
    try:
        from langchain_huggingface import HuggingFaceEmbeddings
    except ImportError as exc:  # pragma: no cover - optional dependency
        try:
            from langchain_community.embeddings import HuggingFaceEmbeddings
        except ImportError:
            raise ProviderError(
                "local embeddings require the 'langchain-huggingface' package "
                "(pip install langchain-huggingface sentence-transformers)"
            ) from exc

    # A local directory must not trigger a network call. Setting this before the
    # model loads keeps an accidentally-offline machine from stalling at startup.
    if offline:
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    return HuggingFaceEmbeddings(
        model_name=model_ref,
        model_kwargs={"device": device},
        encode_kwargs={"batch_size": batch_size, "normalize_embeddings": normalize},
    )


def _build_local_embeddings(settings: Settings) -> Embeddings:
    model_ref, offline = _local_model_reference(
        settings.embedding_local_path, "BAAI/bge-m3"
    )
    logger.info("using local embedding model: %s (device=%s)", model_ref, settings.embedding_device)
    return _cached_local_embeddings(
        model_ref,
        settings.embedding_device,
        settings.embedding_batch_size,
        True,
        offline,
    )


# --------------------------------------------------------------------------- #
# rerankers
# --------------------------------------------------------------------------- #
class BaseReranker(ABC):
    """Reorders candidate documents by relevance to the query.

    Implementations return scores alongside the ordering rather than just an
    ordering, because the score is what lets the caller tell "this passage answers
    the question" apart from "this passage is merely the least irrelevant thing we
    found". That distinction is the basis of the relevance gate.
    """

    name: str = "base"

    #: True when ``rerank`` returns calibrated relevance scores that a threshold can
    #: be applied to. A cross-encoder qualifies; a rank-fusion fallback does not,
    #: because its numbers are ordinal and corpus-specific.
    produces_relevance_scores: bool = False

    @abstractmethod
    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
        fused_scores: list[float] | None = None,
    ) -> tuple[list[int], list[float]]:
        """Return indices into ``documents`` (most relevant first) and their scores.

        Scores are returned in the same order as the indices, so
        ``scores[position]`` belongs to ``indices[position]``.
        """


class NoopReranker(BaseReranker):
    """Keeps the fused order.

    Score semantics differ here: without a cross-encoder there is no comparable
    relevance scale, so the fused score is passed through unchanged. Callers must
    not apply a relevance threshold to these values — see ``score_source``.
    """

    name = "noop"
    produces_relevance_scores = False

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
        fused_scores: list[float] | None = None,
    ) -> tuple[list[int], list[float]]:
        count = min(top_n, len(documents))
        indices = list(range(count))
        scores = (fused_scores or [0.0] * len(documents))[:count]
        return indices, [float(score) for score in scores]


class DashScopeReranker(BaseReranker):
    """Aliyun Model Studio native text-rerank endpoint."""

    name = "dashscope"
    produces_relevance_scores = True

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.api_key = _require_api_key(self.settings)
        self.model = self.settings.reranker_model
        self._endpoint = (
            f"https://dashscope.aliyuncs.com{_DASHSCOPE_RERANK_PATH}"
            if "dashscope.aliyuncs.com" in self.settings.llm_base_url
            else f"https://dashscope-intl.aliyuncs.com{_DASHSCOPE_RERANK_PATH}"
        )

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
        fused_scores: list[float] | None = None,
    ) -> tuple[list[int], list[float]]:
        if not documents:
            return [], []
        payload = {
            "model": self.model,
            "input": {"query": query, "documents": documents},
            "parameters": {"return_documents": False, "top_n": min(top_n, len(documents))},
        }
        try:
            response = httpx.post(
                self._endpoint,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30.0,
            )
            response.raise_for_status()
            results = response.json()["output"]["results"]
        except Exception as exc:  # network, quota, or contract change
            logger.warning("rerank failed, falling back to fused order: %s", exc)
            return NoopReranker().rerank(query, documents, top_n, fused_scores)

        ordered = sorted(
            results, key=lambda item: -float(item.get("relevance_score", 0.0))
        )
        indices = [int(item["index"]) for item in ordered][:top_n]
        scores = [float(item.get("relevance_score", 0.0)) for item in ordered][:top_n]
        return indices, scores


class LocalBGEReranker(BaseReranker):
    """Local cross-encoder reranker.

    Requires the optional ``sentence-transformers`` dependency and downloads model
    weights on first use, so it is not the default.
    """

    name = "local-bge"
    produces_relevance_scores = True

    def __init__(
        self,
        model_name: str = "BAAI/bge-reranker-v2-m3",
        *,
        device: str = "cpu",
        max_length: int = 512,
    ) -> None:
        try:
            from sentence_transformers import CrossEncoder
        except ImportError as exc:  # pragma: no cover - optional path
            raise ProviderError(
                "local-bge reranker requires sentence-transformers; "
                "install it with `pip install sentence-transformers`"
            ) from exc
        # The pairing text can be long; truncating keeps latency predictable without
        # materially changing the ranking for passage-level inputs.
        self._model = CrossEncoder(
            model_name, device=device, max_length=max_length
        )

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int,
        fused_scores: list[float] | None = None,
    ) -> tuple[list[int], list[float]]:
        if not documents:
            return [], []
        raw = self._model.predict([(query, doc) for doc in documents])
        scores = [float(value) for value in raw]
        order = sorted(range(len(scores)), key=lambda i: -scores[i])[:top_n]
        return order, [scores[index] for index in order]


def build_reranker(settings: Settings | None = None) -> BaseReranker:
    """Instantiate the configured reranker, degrading safely if unavailable.

    The result is cached per configuration. Loading a cross-encoder costs seconds, and
    a retrieval layer that rebuilds it per query turns a 0.8s rerank into minutes —
    which is exactly what happened before this cache existed.
    """
    settings = settings or get_settings()
    # Reassigning the parameter name here would shadow the cache key, so the provider
    # string is normalized before it is handed to the cached factory.
    provider = (settings.reranker_provider or "noop").strip().lower()
    return _cached_reranker(
        provider,
        settings.reranker_local_path,
        settings.reranker_model,
        settings.reranker_device,
        settings.dashscope_api_key,
    )


@lru_cache(maxsize=4)
def _cached_reranker(
    provider: str, local_path: str, model: str, device: str, api_key: str
) -> BaseReranker:
    provider = (provider or "noop").strip().lower()

    if provider in {"none", "noop", "off"}:
        return NoopReranker()
    if provider in {"local", "local-bge", "bge"}:
        model_ref, _ = _local_model_reference(
            local_path, model or "BAAI/bge-reranker-v2-m3"
        )
        try:
            return LocalBGEReranker(model_ref, device=device)
        except ProviderError as exc:
            logger.warning("%s; falling back to noop reranker", exc)
            return NoopReranker()
    if provider in {"dashscope", "qwen", "bailian", "aliyun"}:
        try:
            return DashScopeReranker(
                get_settings().model_copy(update={"dashscope_api_key": api_key})
            )
        except ProviderError as exc:
            logger.warning("%s; falling back to noop reranker", exc)
            return NoopReranker()

    logger.warning("unknown reranker provider '%s'; falling back to noop", provider)
    return NoopReranker()
