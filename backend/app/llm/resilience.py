"""Resilient chat model: candidate fallback chain with a three-state circuit breaker.

Pattern migrated from nageoffer/ragent (Apache-2.0), which advertises "模型层级路由 +
首包探测 + 三态断路器" (model tiering with first-packet probing and a three-state
breaker; see its README and ``assets/model-routing-failover.svg``). RAGent stores
model health in Redis; this single-machine version keeps the same state machine
in-process, which is all a solo workbench needs.

Semantics:

* Candidates are ordered: ``LLM_MODEL`` first, then ``LLM_FALLBACK_MODELS``.
* A candidate is *committed to* only once its stream yields a first packet. Any
  failure before that point (provider exception, empty stream) records a failure
  and moves to the next candidate; the httpx read timeout on each client already
  supplies the "first-packet timeout" half of the probing. A failure after the
  first packet cannot be switched away from without emitting a torn answer, so it
  propagates to the caller instead.
* The breaker has three states per model: closed (normal), open (tripped), and
  half-open (one probe allowed after the cooldown). A model that keeps failing is
  skipped without each new request having to hit it first.
* Every fallback switch appends a user-facing notice to ``notices``; the agent
  graph drains these into the SSE stream so the user reads "已切换备用模型"
  instead of a raw vendor error. ``translate_provider_error`` maps the vendor's
  status codes onto actionable Chinese sentences.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, AsyncIterator

from openai import APIConnectionError, APIStatusError, APITimeoutError

from .providers import ProviderError

logger = logging.getLogger(__name__)


def translate_provider_error(exc: BaseException) -> str:
    """Turn a provider exception into one actionable Chinese sentence.

    The raw vendor error ("Error code: 403 - AccessDenied") tells the user nothing
    about what to do; the mapping below is keyed by the statuses this deployment
    has actually hit (a 403 quota exhaustion motivated the whole fallback chain).
    """
    status = getattr(exc, "status_code", None)
    if status == 401:
        return "模型服务认证失败（401）：API Key 无效或已过期，请检查 .env 里的 DASHSCOPE_API_KEY。"
    if status == 403:
        return "模型服务拒绝访问（403）：该 API Key 没有此模型的权限，或配额已用尽，请到模型服务控制台确认。"
    if status == 429:
        return "模型服务限流（429）：请求过于频繁，请稍后重试。"
    if isinstance(exc, APITimeoutError) or isinstance(exc, TimeoutError):
        return "模型请求超时：模型服务响应过慢或网络不稳，请稍后重试。"
    if isinstance(exc, APIConnectionError):
        return "无法连接模型服务：请检查网络；如果系统开着代理，请关闭后重试。"
    if isinstance(exc, APIStatusError):
        return f"模型服务返回错误（{status}）：{exc}"
    return f"模型调用失败：{exc}"


class CircuitBreaker:
    """Three-state breaker (closed / open / half-open) for one model.

    State transitions mirror the classic pattern: ``threshold`` consecutive
    failures trip the breaker open; after ``cooldown`` seconds exactly one probe
    request is let through — success closes the breaker, failure re-opens it for
    another cooldown. Thread-safe because model calls happen on the event loop
    while other threads (indexing, rewriting) may consult the same breaker.
    """

    def __init__(
        self,
        name: str,
        *,
        threshold: int = 3,
        cooldown: float = 60.0,
        clock=time.monotonic,
    ) -> None:
        self.name = name
        self.threshold = threshold
        self.cooldown = cooldown
        self._clock = clock
        self._lock = threading.Lock()
        self.state = "closed"
        self.failures = 0
        self._opened_at = 0.0

    def allow(self) -> bool:
        """Whether a request may use this model now, possibly as a probe."""
        with self._lock:
            if self.state == "closed":
                return True
            if self.state == "half_open":
                # The probe slot is taken; concurrent requests wait it out.
                return False
            if self._clock() - self._opened_at >= self.cooldown:
                # Cooldown elapsed: let exactly one probe through. Staying in
                # half-open until the probe resolves keeps concurrent requests
                # from stampeding a model that is probably still down.
                self.state = "half_open"
                return True
            return False

    def record_success(self) -> None:
        with self._lock:
            self.state = "closed"
            self.failures = 0

    def record_failure(self) -> None:
        with self._lock:
            if self.state == "half_open":
                self.state = "open"
                self._opened_at = self._clock()
                return
            self.failures += 1
            if self.failures >= self.threshold:
                self.state = "open"
                self._opened_at = self._clock()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "model": self.name,
                "state": self.state,
                "failures": self.failures,
            }


# One breaker per model name for the process lifetime: the point of the breaker is
# that a model that failed on the previous request is skipped on the next one.
_BREAKERS: dict[str, CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()


def breaker_for(name: str, *, threshold: int, cooldown: float) -> CircuitBreaker:
    with _BREAKERS_LOCK:
        breaker = _BREAKERS.get(name)
        if breaker is None:
            breaker = CircuitBreaker(name, threshold=threshold, cooldown=cooldown)
            _BREAKERS[name] = breaker
        return breaker


def reset_breakers() -> None:
    """Test hook: breakers are process-global, so tests must start from clean state."""
    with _BREAKERS_LOCK:
        _BREAKERS.clear()


class _BoundResilientModel:
    """The model returned by ``bind_tools``: streams with fallback and notices."""

    def __init__(self, parent: "ResilientChatModel", bound: list[tuple[str, Any]]) -> None:
        self._parent = parent
        self._bound = bound
        # Shared with the parent so callers can drain from either handle.
        self.notices = parent.notices

    async def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        failed: list[str] = []
        last_error: BaseException | None = None

        for name, model in self._bound:
            breaker = breaker_for(
                name,
                threshold=self._parent.threshold,
                cooldown=self._parent.cooldown,
            )
            if not breaker.allow():
                logger.info("model %s is %s, skipping to next candidate", name, breaker.state)
                continue
            try:
                iterator = model.astream(messages, **kwargs)
                first = await iterator.__anext__()
            except StopAsyncIteration:
                # 空响应：provider closed the stream without a single packet.
                breaker.record_failure()
                failed.append(name)
                last_error = ProviderError(f"模型 {name} 返回了空响应")
                logger.warning("model %s returned an empty stream; trying next candidate", name)
                continue
            except Exception as exc:
                breaker.record_failure()
                failed.append(name)
                last_error = exc
                logger.warning(
                    "model %s failed before first packet (%s); trying next candidate",
                    name,
                    exc,
                )
                continue

            breaker.record_success()
            if failed:
                self.notices.append(
                    f"模型 {'、'.join(failed)} 调用失败，已切换备用模型 {name} 继续回答。"
                )
            yield first
            # Past the first packet the answer may already be streaming to the
            # user; a mid-stream failure is surfaced, not silently restarted.
            async for chunk in iterator:
                yield chunk
            return

        if last_error is None:
            raise ProviderError(
                "所有候选模型都在熔断冷却中，暂时不可用；请稍后重试。"
            )
        raise ProviderError(translate_provider_error(last_error)) from last_error


class ResilientChatModel:
    """Drop-in stand-in for a chat model that routes across candidates.

    Exposes the two members the agent graph uses: ``bind_tools`` (whose result
    carries ``astream`` and the ``notices`` list) and ``astream`` for unbound use.
    With a single candidate it behaves exactly like the raw model.
    """

    def __init__(
        self,
        candidates: list[tuple[str, Any]],
        *,
        threshold: int = 3,
        cooldown: float = 60.0,
    ) -> None:
        if not candidates:
            raise ValueError("ResilientChatModel needs at least one candidate model")
        self.candidates = candidates
        self.threshold = threshold
        self.cooldown = cooldown
        self.notices: list[str] = []

    def bind_tools(self, tools: Any, **kwargs: Any) -> _BoundResilientModel:
        bound = [
            (name, model.bind_tools(tools, **kwargs)) for name, model in self.candidates
        ]
        return _BoundResilientModel(self, bound)

    async def astream(self, messages: Any, **kwargs: Any) -> AsyncIterator[Any]:
        async for chunk in _BoundResilientModel(self, list(self.candidates)).astream(
            messages, **kwargs
        ):
            yield chunk
