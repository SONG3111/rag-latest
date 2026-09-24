# 项目工作约定

## 优先参考 GitHub 高质量项目（用户明确要求，长期有效）

凡是**写代码、写系统方案、修比复杂的bug、设计新功能**等任务，动手之前先到 GitHub 找相关的
高质量项目做参考，把成熟方案迁移过来，而不是从零自造（简单的bug，可以自己解决）。本项目已有的实践都遵循这个模式，
例如：

- 系统提示词迁移自 LlamaIndex / RAGFlow（见 `ai-service/app/agent/prompts.py` 模块注释）
- `read_range` 的范围语义迁移自 haris-musa/excel-mcp-server（见 `excel_ops.py` 注释）
- Word 标题判定迁移自 docling 的 `msword_backend._get_label_and_level`（见
  `ai-service/app/retrieval/chunking.py` 的 `_is_heading`）
- Excel 分块的空行分块 + 表头嗅探来自 Excel current-region 语义与业界分表模式

### 执行要点

1. **先搜后写**：用 WebSearch/WebFetch 找该问题的知名开源实现（优先生态主流、
   star 高、维护活跃、测试完善的项目），确认它怎么解决这个问题。
2. **迁移而非照抄**：理解方案后按本项目的分层、错误语义、注释风格适配；结构性借鉴
   在 docstring/注释里注明出处项目与文件。
3. **许可证**：优先 MIT / Apache-2.0 项目；GPL 代码只借鉴思路，禁止逐行复制。
   若从上游复制了代码，在 `mcp-office-server/NOTICE.md`（该包范围内）或对应模块
   注释中记录来源与许可。
4. **迁移必配回归测试**：用测试钉住迁移来的行为（本项目所有迁移修复都有对应
   回归用例，见 `ai-service/tests/`、`backend-java/src/test/`、`mcp-office-server/tests/`）。
5. **找不到合适参考时**：如实说明"没有找到可直接迁移的高质量实现"，然后给出
   自研设计与理由，不要编造参考来源。

## 测试一律用 mock，不调用真实模型（用户明确要求，长期有效）

任何验证、复现、回归**都不得发起真实的 LLM 调用**（不要用 `chat/stream` 跑真实模型，
用户的模型 token 有限）。真实模型调用仅在用户当次明确要求时进行。

替代手段（均已存在，直接用）：

- ai-service 测试套件（`ai-service/tests/`，FastAPI + pytest）：fake LLM / fake
  embeddings / seed 提案 + 真实 MCP 子进程，全程不调用模型；
- backend-java 测试套件（`backend-java/src/test/`，JUnit5 + WireMock 桩掉
  ai-service）：业务端点、SSE 中继与落库，全程不调用模型也不依赖 Python 进程；
- mcp-office-server 测试套件（`mcp-office-server/tests/`，74 个用例）：直接调用
  工具函数与 MCP tool 层；
- 需要多提案审批顺序等 API 级场景时，用 seed 数据直接驱动 REST 接口
  （参考 `ai-service/tests/test_proposal_rebase.py`、`test_api_operations.py`
  与 `backend-java` 中对应的种子提案测试写法）。

服务结构（feat/java-backend 分支起生效）：`backend-java/`（Spring Boot，Java 21，
对外 8000，独占 SQLite）+ `ai-service/`（FastAPI，内网 8001，负责 agent/检索/嵌入/
MCP 子进程）+ `frontend/`（不变）+ `mcp-office-server/`（不变）。

`scripts/approval_order_test.py chat` 这类会触发真实模型的脚本入口仅供用户手动使用，
agent 不要执行 `chat` 子命令。

