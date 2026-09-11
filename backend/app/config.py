"""Application settings.

Every external dependency is configurable so the stack can be repointed at a
different provider (or a local model) without touching business code. The default
profile targets Aliyun Model Studio (百炼) because a single key there covers chat,
embedding, and reranking.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Aliyun Model Studio exposes an OpenAI-compatible surface. The international
# site uses a different host, so both are documented and overridable.
DASHSCOPE_CN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
DASHSCOPE_INTL_BASE_URL = "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- storage ---
    data_dir: Path = Field(default=PROJECT_ROOT / "data")
    database_url: str | None = None

    # --- models ---
    dashscope_api_key: str = Field(default="", alias="DASHSCOPE_API_KEY")
    llm_base_url: str = Field(default=DASHSCOPE_CN_BASE_URL)
    llm_model: str = Field(default="qwen-plus")
    llm_temperature: float = Field(default=0.1)
    llm_request_timeout: float = Field(default=120.0)
    # DashScope's reasoning models stream a `reasoning_content` phase before any
    # answer text. Measured on this corpus: with thinking on the first answer token
    # arrives at ~8.6s, with it off at ~0.7s. For short factual lookups over a small
    # workspace the extra deliberation buys little, so it is off by default. Set to
    # true for questions that benefit from multi-step reasoning.
    llm_enable_thinking: bool = Field(default=False)

    embedding_base_url: str | None = Field(default=None)
    embedding_model: str = Field(default="text-embedding-v3")
    embedding_dimensions: int = Field(default=1024)
    # "dashscope" (cloud) or "local" (sentence-transformers weights on disk).
    embedding_provider: str = Field(default="dashscope")
    embedding_local_path: str = Field(default="")
    embedding_device: str = Field(default="cpu")
    embedding_batch_size: int = Field(default=16)

    reranker_provider: str = Field(default="dashscope")
    reranker_model: str = Field(default="gte-rerank-v2")
    reranker_local_path: str = Field(default="")
    reranker_device: str = Field(default="cpu")
    rerank_top_n: int = Field(default=5)
    # Cross-encoder relevance floor. Candidates below it are dropped, so a question
    # the corpus cannot answer yields an empty result instead of five weak passages.
    # 0 disables the gate.
    #
    # Calibration caveat, because getting this wrong silently breaks recall: the
    # numbers depend on *what text is scored*. With long passages (a whole policy
    # section) in-corpus hits reach ~0.9 and out-of-corpus stays under 0.03, so 0.15
    # separates them cleanly. But scoring long passages costs ~2s per pair on CPU, so
    # retrieval scores the short child text instead. Terse rows ("姓名=李娜,
    # 报销类型=招待费") score far lower across the board — a correct hit may sit near
    # 0.05 and a miss near 0.001 — so the floor has to be recalibrated, not copied.
    # 0.02 is the measured value for the demo corpus; re-run
    # `scripts/eval_retrieval.py --dense --sweep` after changing the corpus, the
    # reranker, or RERANK_CANDIDATES.
    rerank_score_threshold: float = Field(default=0.02)

    # --- retrieval ---
    chunk_size: int = Field(default=700)
    chunk_overlap: int = Field(default=80)
    retrieval_vector_top_k: int = Field(default=20)
    retrieval_bm25_top_k: int = Field(default=20)
    rerank_candidates: int = Field(default=20)

    # --- query rewriting ---
    query_rewrite_enabled: bool = Field(default=True)
    query_rewrite_model: str = Field(default="qwen-turbo")
    query_rewrite_history_turns: int = Field(default=4)
    # Measured rewrite latency is 4-8s per call, so a 15s ceiling was cutting off
    # legitimate slow responses and falling back to the original query. The timeout
    # has to sit above the observed p100 with margin, not at it.
    query_rewrite_timeout: float = Field(default=45.0)

    # --- agent ---
    agent_max_iterations: int = Field(default=12)
    agent_recursion_limit: int = Field(default=40)

    # --- mcp ---
    mcp_server_command: str | None = Field(default=None)
    mcp_tool_timeout: float = Field(default=60.0)

    # --- api ---
    cors_origins: list[str] = Field(
        default=["http://localhost:5173", "http://127.0.0.1:5173"]
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    # --- derived paths ---
    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "workspaces"

    @property
    def backups_dir(self) -> Path:
        return self.data_dir / "backups"

    @property
    def qdrant_dir(self) -> Path:
        return self.data_dir / "qdrant"

    @property
    def effective_database_url(self) -> str:
        if self.database_url:
            return self.database_url
        return f"sqlite:///{(self.data_dir / 'app.db').as_posix()}"

    @property
    def effective_embedding_base_url(self) -> str:
        return self.embedding_base_url or self.llm_base_url

    def workspace_dir(self, workspace_id: str) -> Path:
        return self.workspaces_dir / workspace_id

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.workspaces_dir,
            self.backups_dir,
            self.qdrant_dir,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_directories()
    return settings
