"""WhisperLiveKit adapter for browser PCM streams.

WhisperLiveKit (Apache-2.0) provides the LocalAgreement/VAD pipeline and exposes
confirmed lines separately from the revisable tail. The browser sends raw
16kHz mono s16le PCM, so this integration never invokes FFmpeg or writes an
audio file.
"""

from __future__ import annotations

import asyncio
import importlib.util
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ..config import Settings
from .text import is_probable_hallucination, normalize_transcript_text

logger = logging.getLogger(__name__)


class MeetingASRUnavailable(RuntimeError):
    """Raised when the optional local transcription backend cannot start."""


@dataclass(frozen=True)
class ASRLine:
    start_ms: int
    end_ms: int
    text: str


@dataclass(frozen=True)
class ASRUpdate:
    lines: list[ASRLine]
    partial: str = ""
    status: str = "active_transcription"
    error: str | None = None
    compute_lag_seconds: float = 0.0


class MeetingASRSession(Protocol):
    async def push_audio(self, data: bytes) -> None: ...

    async def stop(self) -> None: ...

    def updates(self) -> AsyncIterator[ASRUpdate]: ...

    async def cleanup(self) -> None: ...


class MeetingTranscriber(Protocol):
    @property
    def ready(self) -> bool: ...

    async def open_session(self, *, language: str) -> MeetingASRSession: ...

    def health(self) -> dict[str, Any]: ...


def build_asr_config(
    settings: Settings,
    *,
    cache_dir: str,
    model_dir: str | None,
) -> Any:
    """Build WhisperLiveKit config against the installed package's field set.

    PyPI 0.2.26 and current GitHub ``main`` do not expose exactly the same
    dataclass fields. Passing an upstream-main-only option to an older wheel
    fails before the model is loaded, so unsupported keys are filtered rather
    than letting an optional tuning field break the whole recording.
    """
    from whisperlivekit import WhisperLiveKitConfig

    kwargs: dict[str, Any] = {
        "backend": settings.meeting_asr_backend,
        "backend_policy": "localagreement",
        "model_size": settings.meeting_asr_model,
        "model_cache_dir": cache_dir,
        "model_dir": model_dir,
        "lan": settings.meeting_asr_language,
        "pcm_input": True,
        "transcription": True,
        "vad": True,
        "vac": True,
        "diarization": False,
        "min_chunk_size": 0.5,
        "buffer_trimming": "segment",
        "buffer_trimming_sec": settings.meeting_asr_buffer_trimming_seconds,
        "pause_segmentation_seconds": (
            settings.meeting_asr_pause_segmentation_seconds
        ),
        "asr_coalesce_min_s": settings.meeting_asr_coalesce_seconds,
        # Present on newer upstream builds; ignored on older PyPI releases.
        "max_buffered_audio": 60.0,
    }
    supported = set(getattr(WhisperLiveKitConfig, "__dataclass_fields__", {}))
    if supported:
        kwargs = {key: value for key, value in kwargs.items() if key in supported}
    return WhisperLiveKitConfig(**kwargs)


class LiveASRSession:
    """Bridge WhisperLiveKit's result generator to a single-consumer async queue."""

    def __init__(
        self,
        processor: Any,
        results: Any,
        *,
        language: str,
        simplify_chinese: bool,
    ) -> None:
        self.processor = processor
        self.results = results
        self.language = language
        self.simplify_chinese = simplify_chinese
        self.queue: asyncio.Queue[ASRUpdate | None] = asyncio.Queue()
        self.pump_task = asyncio.create_task(self._pump())

    def _normalize(self, front: Any) -> ASRUpdate:
        lines: list[ASRLine] = []
        for line in getattr(front, "lines", None) or []:
            text = normalize_transcript_text(
                str(getattr(line, "text", "") or ""),
                language=self.language,
                simplify=self.simplify_chinese,
            )
            if not text:
                continue
            try:
                start = float(getattr(line, "start", 0) or 0)
                end = float(getattr(line, "end", start) or start)
            except (TypeError, ValueError):
                continue
            start_ms = max(0, int(round(start * 1000)))
            end_ms = max(0, int(round(end * 1000)))
            if is_probable_hallucination(
                text, start_ms=start_ms, end_ms=end_ms
            ):
                continue
            lines.append(
                ASRLine(
                    start_ms=start_ms,
                    end_ms=end_ms,
                    text=text,
                )
            )
        partial = normalize_transcript_text(
            str(getattr(front, "buffer_transcription", "") or ""),
            language=self.language,
            simplify=self.simplify_chinese,
        )
        if partial and is_probable_hallucination(
            partial, start_ms=0, end_ms=0
        ):
            partial = ""
        lag = max(
            float(
                getattr(front, "remaining_time_transcription_processing", 0)
                or 0
            ),
            float(getattr(front, "remaining_time_transcription", 0) or 0),
        )
        return ASRUpdate(
            lines=lines,
            partial=partial,
            status=str(getattr(front, "status", "active_transcription") or ""),
            error=str(getattr(front, "error", "") or "").strip() or None,
            compute_lag_seconds=round(max(0.0, lag), 1),
        )

    async def _pump(self) -> None:
        try:
            async for front in self.results:
                update = self._normalize(front)
                await self.queue.put(update)
                if update.status == "error":
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("meeting ASR result stream failed")
            await self.queue.put(
                ASRUpdate(
                    lines=[],
                    status="error",
                    error=f"本地转写失败：{exc}",
                )
            )
        finally:
            await self.queue.put(None)

    async def push_audio(self, data: bytes) -> None:
        await self.processor.process_audio(data)

    async def stop(self) -> None:
        await self.processor.process_audio(b"")

    async def updates(self) -> AsyncIterator[ASRUpdate]:
        while True:
            item = await self.queue.get()
            if item is None:
                return
            yield item

    async def cleanup(self) -> None:
        if not self.pump_task.done():
            self.pump_task.cancel()
        await asyncio.gather(self.pump_task, return_exceptions=True)
        await self.processor.cleanup()


def faster_whisper_transcribe_kwargs(
    settings: Settings,
    *,
    language: str | None,
    init_prompt: str = "",
) -> dict[str, Any]:
    """Return low-latency, hallucination-resistant decode options.

    WhisperLiveKit's installed LocalAgreement backend hardcodes ``beam_size=5``
    and ``condition_on_previous_text=True``. This helper keeps our tuning in the
    integration layer instead of editing site-packages.
    """
    return {
        "language": language,
        # LocalAgreement supplies the tail of confirmed transcript here. Never
        # add natural-language system instructions: Whisper may copy them into
        # the recognized text.
        "initial_prompt": init_prompt.strip(),
        "beam_size": max(1, settings.meeting_asr_beam_size),
        "word_timestamps": True,
        "condition_on_previous_text": (
            settings.meeting_asr_condition_on_previous_text
        ),
        "no_speech_threshold": settings.meeting_asr_no_speech_threshold,
        "log_prob_threshold": settings.meeting_asr_log_prob_threshold,
        "compression_ratio_threshold": (
            settings.meeting_asr_compression_ratio_threshold
        ),
        "hallucination_silence_threshold": (
            settings.meeting_asr_hallucination_silence_threshold
        ),
    }


def tuned_faster_whisper_class(settings: Settings):
    """Build a WhisperLiveKit-compatible faster-whisper backend subclass."""
    from whisperlivekit.local_agreement.backends import FasterWhisperASR

    if getattr(FasterWhisperASR, "_meeting_tuned", False):
        return FasterWhisperASR

    configured_device = settings.meeting_asr_device or "auto"
    configured_compute = settings.meeting_asr_compute_type or "auto"
    no_speech_threshold = settings.meeting_asr_no_speech_threshold

    class MeetingFasterWhisperASR(FasterWhisperASR):
        _meeting_tuned = True

        def load_model(self, model_size=None, cache_dir=None, model_dir=None):
            from faster_whisper import WhisperModel
            from whisperlivekit.model_paths import resolve_model_path

            if model_dir is not None:
                model_size_or_path = str(resolve_model_path(model_dir))
            elif model_size is not None:
                model_size_or_path = model_size
            else:
                raise ValueError("Either model_size or model_dir must be set")
            return WhisperModel(
                model_size_or_path,
                device=configured_device,
                compute_type=configured_compute,
                download_root=cache_dir,
            )

        def transcribe(self, audio, init_prompt: str = "") -> list:
            options = faster_whisper_transcribe_kwargs(
                settings,
                language=self.original_language,
                init_prompt=init_prompt,
            )
            options.update(self.transcribe_kargs)
            segments, _info = self.model.transcribe(audio, **options)
            return list(segments)

        def ts_words(self, segments):
            from whisperlivekit.timed_objects import ASRToken

            tokens: list[ASRToken] = []
            for segment in segments:
                if (
                    getattr(segment, "no_speech_prob", 0.0)
                    > no_speech_threshold
                ):
                    continue
                for word in segment.words or []:
                    tokens.append(
                        ASRToken(
                            word.start,
                            word.end,
                            word.word,
                            probability=word.probability,
                        )
                    )
            return tokens

    return MeetingFasterWhisperASR


class WhisperLiveKitTranscriber:
    """Lazily loaded singleton ASR engine shared across meeting sessions."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._engine: Any | None = None
        self._load_lock = asyncio.Lock()
        self._loading = False
        self._error: str | None = None
        self._device = settings.meeting_asr_device or "auto"
        self._compute_type = settings.meeting_asr_compute_type or "auto"

    @property
    def ready(self) -> bool:
        return self._engine is not None and not self._loading and not self._error

    def health(self) -> dict[str, Any]:
        available = importlib.util.find_spec("whisperlivekit") is not None
        return {
            "enabled": self.settings.meeting_enabled,
            "available": available,
            "ready": self.ready,
            "loading": self._loading,
            "backend": self.settings.meeting_asr_backend,
            "model": self.settings.meeting_asr_model,
            "language": self.settings.meeting_asr_language,
            "device": self._device,
            "compute_type": self._compute_type,
            "error": self._error,
        }

    def _load_engine_sync(self) -> Any:
        if not self.settings.meeting_enabled:
            raise MeetingASRUnavailable("meeting assistant is disabled")
        try:
            from whisperlivekit import TranscriptionEngine
        except ImportError as exc:
            raise MeetingASRUnavailable(
                "本地转写依赖未安装：请执行 pip install -r backend/requirements.txt"
            ) from exc

        cache_dir = Path(self.settings.data_dir) / "models" / "whisper"
        cache_dir.mkdir(parents=True, exist_ok=True)
        model_dir = self.settings.meeting_asr_model_dir.strip() or None

        config = build_asr_config(
            self.settings,
            cache_dir=str(cache_dir),
            model_dir=model_dir,
        )

        # Replace WhisperLiveKit's LocalAgreement backend class in memory. The
        # subclass keeps device/compute configuration and adds low-latency,
        # hallucination-resistant decode parameters without editing the package.
        if self.settings.meeting_asr_backend == "faster-whisper":
            from whisperlivekit.local_agreement import backends as backend_module
            from whisperlivekit.local_agreement import (
                whisper_online as online_module,
            )

            tuned = tuned_faster_whisper_class(self.settings)
            backend_module.FasterWhisperASR = tuned
            online_module.FasterWhisperASR = tuned

        return TranscriptionEngine(config=config)

    async def _ensure_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        async with self._load_lock:
            if self._engine is not None:
                return self._engine
            self._loading = True
            self._error = None
            try:
                self._engine = await asyncio.to_thread(self._load_engine_sync)
            except Exception as exc:
                self._error = str(exc)
                logger.exception("failed to load meeting ASR engine")
                raise MeetingASRUnavailable(str(exc)) from exc
            finally:
                self._loading = False
            assert self._engine is not None
            return self._engine

    async def open_session(self, *, language: str) -> MeetingASRSession:
        engine = await self._ensure_engine()
        try:
            from whisperlivekit import AudioProcessor

            processor = AudioProcessor(
                transcription_engine=engine,
                language=(language or self.settings.meeting_asr_language),
                mode="full",
                pcm_input=True,
            )
            results = await processor.create_tasks()
        except Exception as exc:
            self._error = str(exc)
            raise MeetingASRUnavailable(str(exc)) from exc
        return LiveASRSession(
            processor,
            results,
            language=language or self.settings.meeting_asr_language,
            simplify_chinese=self.settings.meeting_simplify_chinese,
        )
