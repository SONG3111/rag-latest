# Java 后端迁移进度（feat/java-backend 分支）

> 交接文档 · 2026-09-24。目标架构：**Java 业务后端（Spring Boot，对外 8000，独占 SQLite）
> + Python AI 服务（FastAPI，内网 8001，负责 agent/检索/嵌入/MCP 子进程）**，前端与
> mcp-office-server 不动，对前端的 REST 路径与 SSE 帧契约保持兼容。
> 完整设计见已批准的计划（会话记录），本文档只记进度与可执行结论。

## 一、总览

| 里程碑 | 状态 | 提交 |
|---|---|---|
| M0 骨架并行（分支/改名/Java 骨架/三服务 compose） | ✅ 完成 | `105e121`（纯改名）、`46315e3` |
| M1 数据层 + CRUD + 内部 /v1 接缝 | ✅ 完成 | `078d7ee` |
| M2 聊天链路（无状态化 + SSE 中继） | ✅ 完成（未提交，待用户确认提交） | — |
| M3 审批全链路 + 索引编排 | ⬜ 未开始 | — |
| M4 退役旧代码 + 文档 | ⬜ 未开始 | — |

当前分支 `feat/java-backend`（基于 `dev`）。M2 后两侧测试状态：
**backend-java 25 用例全绿（新增 ChatStreamTests 7 用例）；ai-service 217 用例全绿**
（含 9 个 M1 内部契约 + M2 新增 test_internal_chat_stream.py 5 用例与
Retriever corpus_loader 2 用例）。

## 二、已完成内容

### M0：骨架与基建（46315e3）

- `git mv backend ai-service` 纯改名提交（保历史）。
- `backend-java/`：Spring Boot **4.1.1** / JDK 21 / Maven；`/health` 聚合 ai-service 状态
  （保持原响应形状 + 新增 `ai_service` 字段）；WebClient（仅引 spring-webflux 做 SSE 中继，
  noProxy 对应 Python 版 trust_env=False）；Maven wrapper（含 `.mvn/wrapper/maven-wrapper.jar`，
  type=bin）。
- `ai-service/Dockerfile`（原根 Dockerfile 迁移，改听 **8001**）；`backend-java/Dockerfile`
  （maven 构建 → temurin:21-jre）；`docker-compose.yml` 三服务：`backend`(java,8000,服务名不变
  保住 nginx.conf) + `ai-service`(8001,仅内网) + `frontend`。
- `scripts/run_all_tests.py` 纳入 backend-java 套件（`java -cp wrapper.jar ...`，无 shell）；
  11 个手动脚本的 `ROOT / "backend"` 路径改为 `ROOT / "ai-service"`；README/AGENTS.md 同步。

### M1：数据层 + CRUD（078d7ee）

**Java 侧（backend-java）新增：**

| 文件 | 职责 |
|---|---|
| `db/DbInitializer.java` | 建表 DDL + 只加不改增量迁移（移植 migrations.py；**补列先于索引**——修复了旧库先建 `ix_chunks_parent_id` 会炸的顺序问题）；兼容 Python 版建的旧 app.db |
| `store/WorkspaceRepository` 等 4 个仓储 | JdbcClient 实现；id 用 uuid hex（同 Python 形态）；JSON 列按文本存取 |
| `api/WorkspaceController` | 工作区/文件 CRUD、上传（M1 落盘登记 pending，**索引 M3 接**）、下载、删除（向量清理经 ai-service，失败降级告警） |
| `api/MessageController` / `ToolsController` | 消息列表/反馈、trace 聚合与明细；tools/preview 透传 ai-service |
| `service/FileStorage` | 移植 files.py 存储半边：文件名净化、去重 `(1)`、sha256、沙箱路径解析 |
| `internal/AiServiceClient` | /v1/tools、preview、collections 清理；上游错误转同状态码 ApiException（detail 透传） |
| `config/DataDirEnvironmentPostProcessor` | DataSource 初始化前建 data 目录（spring.factories 注册） |

配置要点：全局 `SNAKE_CASE` 序列化（对齐 Python 契约）；SQLite `WAL/busy_timeout=30000/
foreign_keys` 经 Hikari `data-source-properties` 下发；`rag.ai-service.base-url` 等 env 可覆盖。

**ai-service 侧新增（`app/api/internal.py`，main.py 已注册）：**

- `GET /v1/tools`：MCP 工具清单（含审批注解），按名排序
- `POST /v1/tools/call`：`{tool, arguments}` → 执行 MCP 工具返回 `{"ok":...}` 信封
  （写工具直达文件系统；审批门在 Java 业务层，此端点仅内网）
- `GET /v1/workspaces/{id}/preview?file=&location=`：引用出处读窗口
- `DELETE /v1/collections/{id}` 与 `DELETE /v1/collections/{id}/vectors?file_id=`：
  清集合/按文件清向量（失败 502，调用方降级告警）

**测试：** `WorkspaceApiTests`（13 用例，真实 SQLite 临时目录 + WireMock 桩 ai-service）、
`DbInitializerTests`（旧库补列回归 + 新库建表）、health 上下/下游 2 用例；
Python `test_internal_tools.py`（9 用例，真实 MCP 子进程）。

## 三、M2 设计结论（已敲定，已按此实现）

无状态聊天契约的**全部关键决策**已分析完毕（精读了 routes.py chat_stream、graph.py
依赖点、memory.py、pipeline.py、operations.py），实现已全部落地（见 §二 M2 清单）：

### 3.1 WorkspaceAgent 改造（最小侵入）

- 构造函数**保持兼容**：`WorkspaceAgent(session=None, workspace=None, client, settings=None, *, llm=None, runtime=None)`，
  新增可选 `runtime`。15 处测试调用点 `WorkspaceAgent(temp_session, workspace, client, llm=...)` **零改动**。
- `runtime`（新增 dataclass `AgentRuntime`）携带：`workspace_id`、`tracked_files: set[str]`
  （替代 `_scope_listing` 的 DB 查询）、`corpus_loader`（替代 Retriever 的 session）、
  `proposals: list[dict]`（本轮提案 spec 收集，替代 create_operation 落库）、
  `compaction`（overflow 压缩所需的 rows/summary/covered）。
- `_gated_write_tool.propose()`：runtime 模式下不写库——用 `summarize_operation` +
  `extract_target_path`（operations.py 纯函数）组 spec `{tool_name, rel_path, arguments, diff, summary}`
  存入 `runtime.proposals`；返回信封 data 带 `arguments`（tools_node 的 proposal 事件由此取全字段），
  `operation_id` 置 null（**由 Java 生成并替换**后落库再转发帧）。
- overflow 强制压缩（graph.py ~L897）：runtime 模式改为调 3.3 的纯压缩函数，结果回写
  runtime（persist 帧带回新摘要）。

### 3.2 检索语料来源（Python→Java 内部读端点）

- Retriever 加 `corpus_loader` 参数（默认 None 走原 DB 路径，老 REST 与老测试不受影响）。
- **Java 新增内部端点** `GET /internal/workspaces/{id}/retrieval-corpus`
  → `{files: {file_id: rel_path}, chunks: [{id, file_id, parent_id, level, text, location, meta,
  token_counts, token_length, ordinal}]}`（children+parents 全量，corpus 规模小）。
  Python 侧用 httpx 拉取（每次 search 调用一次即可）。
  这样 **app.db 只被 Java 进程打开**（"Java 独占数据库"的最干净解释）。

### 3.3 记忆压缩拆纯函数（memory.py）

- `compact_memory` 拆出纯函数 `compute_compaction(rows, existing_summary, covered, settings, *,
  summarizer=None, force=False) -> (summary, covered_count) | None`（rows 鸭子类型：
  有 `.role/.content/.tool_calls` 即可，请求里的 pydantic 消息对象直接可用）。
- 老 `compact_memory(session,...)` 变成薄包装（读 DB 行 → 纯函数 → 写行）。

### 3.4 ai-service 新端点 `POST /v1/chat/stream`（建议新文件 `app/api/internal_chat.py`）

请求（Java 预组装，注意：**裁剪策略留在 Python**——因为压缩本来就需要全量消息，
原计划"Java 预裁剪历史"从简，见 3.6 取舍）：

```
{workspace_id, run_id, message, timeout_seconds,
 messages: [{role, content, tool_calls}]  // 全量升序，Java 封顶 500 条
 total_messages: int                       // 超过封顶时的总数（covered 算术用）
 summary: str|null, covered_count: int,
 stale_files: [{rel_path, changed_at}],    // Java 计算（见 3.5）
 files: [rel_path]}                        // tracked 文件清单（scope_listing 用）
```

Python 端内完成：intent 门控（需最近 4 条消息，从请求取）、历史裁剪（移植
`_to_langchain_history` + `uncovered_count` 逻辑到请求消息上）、compose_answer、
followups、非强制压缩（在请求消息 + 本轮 user/assistant 合成行上跑 `compute_compaction`）。

SSE 帧分两类：
- **可转发帧**（原样给前端）：token/thinking/notice/tool_call/tool_result/citations/followups/
  proposal（**扩展**携带 `arguments`，`operation_id` 为 null）
- **内部帧**：
  - `persist`（最后一帧，Java 不转发）：`{status: ok|error|timeout, content, citations,
    proposals, trace_nodes, summary: {text, covered_count}|null}`
  - Python **不发 done**；Java 收到 persist 后落库并合成权威 done（带 `message_id/run_id`）
  - error 帧：可转发（原样），随后 persist(status=error)
  - 超时：Python 发 notice + persist(status=timeout)；Java 转发 notice、落库、合成 done
  - 客户端断开：Python 直接停（发不出帧）；**Java 用已转发的 token 快照 + 移植的
    compose_answer 持久化部分答案**并加"（已停止生成…）"注记
  - 取舍：取消路径 trace 节点会丢（旧版有后台任务兜底）；罕见路径，接受并记录
- 提案帧处理：Java 拦截 → 生成 operation id → 建 Operation 行（message_id 保持 null，
  与旧行为一致——旧代码 create_operation 也从不传 message_id）→ 帧里替换 id → 转发

### 3.5 Java 侧新增

- `ChatController`：`POST /api/workspaces/{id}/chat/stream` → SseEmitter；WebClient
  `bodyToFlux(ServerSentEvent)` 中继 + 帧拦截；超时 = `rag.ai-service.chat-turn-timeout`
  （两侧对齐 300s）；取消/超时路径的 compose_answer + PROPOSAL_NOTICE 移植
  （`"⚠️ 已生成 {count} 条待确认的修改提案，请在右侧确认后才会写入文件。"`、
  空回答兜底 `"模型这次没有返回内容，请再说一次。"`）
- `ConversationSummaryRepository`（get/upsert）
- `MessageRepository.listAllAsc(ws, 500)`；stale files 计算（移植
  `_files_changed_since_last_turn`：indexed_at/created_at/mtime 取最大 vs 最后一条消息时间，
  Java 用 Instant 直接比，语义同 Python 的 UTC-naive 比较）
- `/internal/workspaces/{id}/retrieval-corpus`（ChunkRepository + file map）
- 测试：Java 用 WireMock 回放帧序列（proposal→落库、persist→落库+done 合成、error/超时、
  断开持久化部分答案）；Python 把 ScriptedLLM 套件对准 /v1/chat/stream（老 /api 路由
  在 M4 前保持原样可跑，runtime 走 legacy 适配）

### 3.6 与已批准计划的偏差（实现时按此，别再犹豫）

1. 历史裁剪从"Java 预裁剪"改为"Java 送全量、Python 裁剪"——压缩需要全量消息，
   一次传输避免两套裁剪逻辑；核心决策（Java 独占 DB、Python 无状态）不变。
2. preview 走 ai-service 的 `/v1/workspaces/{id}/preview` 透传，而非计划里"经 tools/call
   自行组读窗口"——避免在 Java 重复实现 location 解析（140 行 + 正则契约）。
3. 检索语料经 Java `/internal/...` 反向读取，而非塞进聊天请求。

## 四、待完成清单

### M2 ✅ 已全部实现（2026-09-24，未提交）

按依赖顺序逐项落地，与 §三 设计一致；新增文件/改造清单：

| 侧 | 文件 | 内容 |
|---|---|---|
| ai-service | `app/services/memory.py` | `compute_compaction` 纯函数（rows 鸭子类型）；`compact_memory` 变薄包装；`_build_prompt` role 比较改 `==`（兼容字符串 role） |
| ai-service | `app/retrieval/pipeline.py` | `ChunkRecord` 数据类 + `Corpus` 类型别名；Retriever 加 `corpus_loader`（实例级 memoize：children/parents/file map 共享一次回读），默认 None 走原 DB 路径 |
| ai-service | `app/agent/graph.py` | `AgentRuntime` dataclass；构造函数 session/workspace/client 全可选 + `runtime` 关键字参数（老调用点零改动）；`_workspace_id` 属性统一取 id；propose 的 runtime 分支（spec 进 runtime.proposals，信封带 arguments、operation_id null）；`_scope_listing` 用 tracked_files；知识工具走 corpus_loader；overflow 强制压缩走 compute_compaction 并回写 runtime；tools_node 的 proposal 事件带 arguments |
| ai-service | `app/api/internal_chat.py`（新） | `POST /v1/chat/stream`：请求模型、covered 尾部算术、`_to_langchain_history` 移植（字符串 role）、intent 门控、可转发帧原样转发、persist 末帧（content/citations/proposals/trace_nodes/summary；非强制压缩 + overflow 摘要合并）、超时/错误/取消路径 |
| ai-service | `app/config.py` + `app/main.py` | `java_backend_base_url` 配置；internal_chat 路由注册 |
| ai-service | `tests/test_internal_chat_stream.py`（新） | 5 用例：persist 收尾无 done、历史来自请求、提案带全参且不落库、provider 崩溃后占位 persist、无 MCP 503 |
| ai-service | `tests/test_retrieval_pipeline.py` | +2 用例：corpus_loader 替代 session（单次回读）、file map 缺失兜底"未知文件" |
| backend-java | `store/ChunkRepository.java`（新） | 语料回读（file map + children/parents 全量） |
| backend-java | `api/InternalController.java`（新） | `GET /internal/workspaces/{id}/retrieval-corpus`（404 守卫） |
| backend-java | `store/ConversationSummaryRepository.java`（新） | get/upsert（upsert = UPDATE 命中 0 行再 INSERT） |
| backend-java | `store/OperationRepository.java`（新） | M2 最小面：insertProposed + listByWorkspace（apply/reject/revert 留 M3） |
| backend-java | `store/MessageRepository.java` | +countAll / latestCreatedAt（MAX(created_at)，cutoff 用） |
| backend-java | `service/Answers.java`（新） | compose_answer/PROPOSAL_NOTICE/EMPTY_FALLBACK/STOPPED_NOTE 逐字移植 |
| backend-java | `service/StaleFileDetector.java`（新） | `_files_changed_since_last_turn` 移植（indexed_at/created_at/mtime 取最大 vs cutoff，UTC） |
| backend-java | `internal/AiServiceClient.java` | +streamChat（WebClient SSE Flux，不设超时，调用方 Flux.timeout 控制） |
| backend-java | `api/ChatController.java`（新） | SSE 中继：预组装请求（封顶 500 条 + total + 摘要书签 + stale files + files）、proposal 帧拦截落库回填、persist 权威落库（assistant 行 + trace 节点 + 摘要 upsert）后合成 done（带 message_id/run_id）、中断路径（超时/断开/上游断流）用 token 快照 + Answers.compose 持久化部分答案；`ChatTurnState` 全 synchronized |
| backend-java | `api/dto/Dtos.java` | +ChatRequest/CorpusChunk/CorpusResponse/SummaryRow |
| backend-java | `tests/.../ChatStreamTests.java`（新） | 7 用例：proposal 拦截落库+done 合成、persist 纯文本、摘要书签随请求、超时 persist、上游断流兜底、404/400 守卫、语料回读端点 |

M2 期间顺手修的 M0/M1 遗留问题：

1. **`WorkspaceRepository.exists()` 恒为 false**（`Boolean.TRUE.equals(Long 计数)` 类型永不相等）——
   M2 的 ChatController 首次在正路径调用它才暴露；旧有测试只测过 404 反例。
2. **Spring 7 JdbcClient 的 `query(...)` 是惰性的**，必须 `.list()`/`.single()` 才执行——
   ChunkRepository 的 file map 查询漏 `.list()` 导致 files 恒空（已修，勿再犯）。
3. **`scripts/run_all_tests.py`**：`cwd` 仍指向已改名的 `backend/`；直跑 wrapper jar 缺
   `-Dmaven.multiModuleProjectDirectory` 导致 Maven 启动器报错。两处已修，一键三套件回归可用。
4. `WorkspaceApiTests.messagesListIsChronologicalAndHonorsLimit` 偶发：created_at 毫秒精度
   同值时排序退化到随机 id；测试插入间加 5ms 隔离（老套件自身的 flake，非语义变更）。

### M3

- ai-service：`POST /v1/operations/rebase-plan`（移植 `_plan_rebase`/`_refresh_chained_digests`/
  `_rebase_arguments` 等，输入=待重排提案 JSON+已应用操作，输出=新参数/不可重排清单；
  `test_proposal_rebase.py` 核心用例随迁）与 `POST /v1/index`（chunk+embed+Qdrant upsert，
  回传 chunk 行由 Java 落库；对应移植 index_file 的"嵌入在写锁外"纪律——Java 侧只需
  顺序保证：先调 /v1/index 拿行，再事务写入）
- Java：`GET /operations`（status_filter）、`apply`（备份 backup 服务移植 services/backup.py →
  经 /v1/tools/call 执行 → rebase-plan 改排 → 摘要刷新 → 后台 reindex）、`reject`、`revert`
  （还原备份）；上传/reindex 端点接 /v1/index；`OperationRepository`
- 测试：Java 种子提案驱动 REST（对标 test_api_operations.py）+ Python rebase 单测 → 提交

### M4

- 删 ai-service 死代码：`api/routes.py` 旧 REST、db.py/models.py SQLAlchemy、migrations.py、
  services 中已迁移半边、requirements 瘦身；conftest 夹具相应收敛
- README 架构图/快速开始终稿、AGENTS.md 测试命令核对、`docs/` 新增架构迁移文档
  （含参考来源：未找到可直接照搬的 Java+Python 混合 RAG 仓库，采用通用网关模式；
  备查 modelcontextprotocol/java-sdk、Spring AI——MCP 留 Python 未引入）
- `check_server.py`/`check_chat_http.py` 等手动脚本改造成双进程编排（或明确弃用）
- docker compose 冒烟 + `python scripts/run_all_tests.py` 全绿 → 提交

## 五、环境与命令备忘

- **JDK 21.0.10** 已装（Oracle，PATH 可见）。**Maven 未全局安装**，用 IDEA 自带：
  `"/c/Program Files/JetBrains/IntelliJ IDEA 2026.1/plugins/maven/lib/maven3/bin/mvn"`
  （3.9.11）；仓库自包含 wrapper：`backend-java/mvnw(.cmd)` 或
  `java -cp .mvn/wrapper/maven-wrapper.jar org.apache.maven.wrapper.MavenWrapperMain <goal>`。
- 测试：`cd backend-java && mvn test`；`cd ai-service && ../.venv312/python.exe -m pytest`；
  `cd mcp-office-server && ../.venv312/python.exe -m pytest tests -q`；
  一键：`./.venv312/python.exe scripts/run_all_tests.py`。
- 本地起服务（仓库根）：ai-service 先起
  `$env:PYTHONPATH="ai-service"; ./.venv312/python.exe -m uvicorn app.main:app --reload --port 8001`，
  Java 后起（占用 8000 对外）。

### Boot 4 踩坑记录（继续开发会再遇到）

- Jackson 3：包名 `tools.jackson.*`（JsonNode/ObjectMapper/JsonNodeFactory）。
- `ReactorClientHttpConnector` 在 `org.springframework.http.client.reactive`。
- `@WebMvcTest` → 独立模块 `spring-boot-starter-webmvc-test`，包名
  `org.springframework.boot.webmvc.test.autoconfigure`。
- `TestRestTemplate`（spring-boot-resttestclient）4.1.1 的自动配置类内省异常，**别用**；
  测试里注入 `org.springframework.boot.web.server.context.WebServerApplicationContext`
  自建 RestTemplate + 不抛错的 ResponseErrorHandler（Framework 7 该接口只有
  `hasError(ClientHttpResponse)` 一个抽象方法）。
- JUnit `@TempDir` 静态字段注入晚于 `@DynamicPropertySource`：临时目录用静态初始化块
  `Files.createTempDirectory`。
- `@DynamicPropertySource` 里引用 WireMock 端口是标准做法，先 start 再注册属性。

### Mimosa hook 约定（本仓库写入会经过安全扫描）

- 禁止 Bash 直写源码（sed/cat > 文件），必须 Write/Edit 工具。
- 测试/脚本里动态拼接 JDBC URL、裸 `Statement.execute` 会被判高危拦截——
  用固定 URL 字面量 + JdbcTemplate（DDL 用 text block 字面量，数据行参数化）。
- `subprocess` 必须字面量参数列表 + `shell=False`（`cmd /c` 会被拦）。
- 提交信息里包含 `scripts/xxx.py` 路径字样会误触发拦截，写提交信息时避开。
- 提交时 hook 反复提示一个**既有**低危项：`ai-service/tests/test_citation_preview.py:3`
  疑似命令注入（原有测试代码，非本次迁移引入，待用户决定是否处理）。

## 六、当前未提交的工作区状态

M2 全部代码已写完并双套件全绿（backend-java 25 / ai-service 217 / mcp 169+1 跳过），
**尚未提交**（用户规则：git 操作需先确认）。改动范围：§四 M2 清单所列 ai-service 5 个
源文件 + 2 个测试文件、backend-java 10 个新文件 + 3 个改动 + 1 个测试、
`scripts/run_all_tests.py`、`WorkspaceApiTests`（flake 修复）、`MIGRATION-PROGRESS.md`。
提交后把 §一 M2 行的提交号补上。
