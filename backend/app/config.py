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
    # 备用模型候选，逗号分隔（如 "qwen-flash,qwen-turbo"）。主模型（llm_model）在拿到
    # 首个流式分片前失败时按序切换；进程内三态熔断器会记住连续失败，冷却期内直接跳过。
    llm_fallback_models: str = Field(default="")
    # 连续失败多少次后熔断主模型（在冷却期内跳过，不再先撞一次死模型）。
    llm_breaker_threshold: int = Field(default=3)
    # 熔断冷却秒数：冷却结束后放一次探测请求，成功即恢复主模型。
    llm_breaker_cooldown: float = Field(default=60.0)

    @property
    def fallback_model_list(self) -> list[str]:
        return [
            name.strip()
            for name in (self.llm_fallback_models or "").split(",")
            if name.strip()
        ]

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
    # Word body children are sized in *tokens*, measured with the embedding
    # model's own tokenizer, so a chunk's encoded length is what the vector
    # store actually sees. bge-m3 guidance puts retrieval units at <=512 tokens;
    # the 512/64 pair was then validated by measurement (grid sweep over
    # CMRC 2018 + the business corpus, docs/rag-test-report): 256/384 lose a
    # pressure case and cost +21-71% children, 768 ties recall but triples
    # mid-sentence cuts. Re-run scripts/eval_chunking.py before changing these.
    chunk_size_tokens: int = Field(default=512)
    chunk_overlap_tokens: int = Field(default=64)
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
    # 单轮问答的全局超时（秒）。一次工具循环可能包含多次模型调用（每次各有
    # llm_request_timeout 的上限），所以全局上限必须明显高于单次请求超时：
    # 12 次迭代 × 单次 120s 的理论上限不现实，但 120s 的"单轮"预算会被两次
    # 正常的慢调用击穿，这里取 300s 兜底真正的卡死场景。
    chat_turn_timeout: float = Field(default=300.0)

    # --- 会话记忆压缩 ---
    # 组装历史时保留的最近原文条数上限（硬上限）；更早的轮次滚动压缩成持久摘要。
    memory_recent_messages: int = Field(default=20)
    # 近窗原文的 token 预算（pi-mono compaction 的 keepRecentTokens）：从最新一条
    # 向前累积估算 token，超出预算的更早消息折叠进摘要或当轮被截断。肥消息（长
    # 答复、大段粘贴）会把窗口压到条数上限以内——条数与 token 消耗没有稳定换算
    # （deepseek-harness compaction-basic 自述其平价 4 字符/token 低估中文），
    # 所以窗口边界必须用 token 表达；估算器见 services/token_budget.py。
    memory_keep_recent_tokens: int = Field(default=8000)
    # （摘要 + 近窗）进入模型的总 token 预算。pi 用 contextTokens > window -
    # reserveTokens 按模型窗口反推；单机要面对不同窗口的候选模型，直接配置总额
    # 更稳。超过即视为 token 压力，触发一次后台压缩。
    memory_context_token_budget: int = Field(default=24000)
    # 批量折叠门槛：消息总数超过该值才走"条数"路径发起摘要（摊薄小模型调用）。
    # token 压力路径不受此门槛限制——压力是当前的事。细消息的长对话仍靠这条路径
    # 把开头内容卷进摘要，否则又回到"长对话锚在开头/丢开头"的老问题。
    memory_compact_trigger: int = Field(default=40)
    # 摘要字数上限。结构化模板分段多，原来的 300 字偏紧。
    memory_summary_max_chars: int = Field(default=600)

    # --- 意图门控 ---
    # 进 Agent 循环前做一次廉价意图分类（规则优先，模糊时用改写小模型）：
    # 闲聊直接回答不检索；查询意图只暴露只读工具（写工具遮蔽）；
    # 判定拿不准或分类失败时保守走完整链路（宁可多检索不可漏检索）。
    intent_gate_enabled: bool = Field(default=True)

    # --- 推荐后续问题 ---
    # 回合结束时用改写小模型预测 2~3 个用户最可能的追问，SSE followups 事件
    # 送达前端以建议 chip 展示；失败即跳过，不影响对话。false 关闭。
    followups_enabled: bool = Field(default=True)

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

    @field_validator("chunk_overlap_tokens")
    @classmethod
    def _overlap_must_fit_in_chunk(cls, value: int, info) -> int:
        # A hand-splitter would silently misbehave; fail fast instead.
        chunk_size = info.data.get("chunk_size_tokens")
        if chunk_size is not None and value >= chunk_size:
            raise ValueError(
                f"CHUNK_OVERLAP_TOKENS ({value}) must be smaller than "
                f"CHUNK_SIZE_TOKENS ({chunk_size})"
            )
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
