# 工作区文档 Agent（MCP + LangGraph + Vue）

一个本地运行的工作台：上传 Excel / Word，然后用自然语言与 Agent 对话。Agent 自己决定是去知识库里检索，还是直接读写文档；**所有文件操作都通过独立的 MCP Server 提供**，所有写入都必须先给你看变更预览、由你确认后才落盘。

## 它解决什么问题

日常办公里有一类重复劳动：对着制度文件查规定，再回到表格里逐行核对、修改。
这个项目把这条链路交给 Agent 完成，并且保证过程可控——改动看得见、可拒绝、可还原。

> **想深入看设计与取舍？** [`docs/`](docs/README.md) 里有四篇文档：
> 架构详解、端到端流程、技术选型对比（为什么选它不选那个），以及一份落地改进计划。

## 功能

- **知识库问答**：上传的文档自动分块入库，混合检索后带出处回答（`[文件名 · 位置]`）。
- **对话式改文档**：Agent 自主调用 Excel / Word 工具，改值、插行、写公式、查找替换。
- **人机确认闭环**：写入类工具不会直接执行，先生成变更提案与逐项 Diff，确认后才写入，写入前自动备份。
- **工作区隔离**：每个工作区是磁盘上一个独立目录，Agent 的所有路径都锁在沙箱内。
- **工具可见**：界面上能看到 Agent 正在调用哪个工具，以及当前启用的完整工具清单与审批级别。

## 架构

```
┌──────────────────────┐        SSE        ┌──────────────────────────┐
│  Vue 3 + Vite + Pinia│ ────────────────▶ │  FastAPI                 │
│  三栏：文件 / 对话 /  │ ◀──────────────── │  REST + SSE              │
│       引用与待确认    │                   └──────────┬───────────────┘
└──────────────────────┘                              │
                                                      ▼
                                        ┌──────────────────────────────┐
                                        │  LangGraph Agent（工具循环） │
                                        └───────┬──────────────┬───────┘
                                                │              │
                    ┌───────────────────────────▼──┐    ┌──────▼─────────────────┐
                    │ 知识库检索工具                │    │ MCP 工具（stdio 子进程）│
                    │ 向量 + BM25 → RRF → 重排      │    │ Excel / Word 读写       │
                    └───────────┬──────────────────┘    └──────┬─────────────────┘
                                │                              │
                    ┌───────────▼──────────┐        ┌──────────▼──────────────┐
                    │ Qdrant(local) + SQLite│        │ 工作区目录（沙箱）       │
                    └──────────────────────┘        └─────────────────────────┘
```

### 三个值得说明的设计决定

**1. 文档工具做成 MCP Server，而不是普通函数**

Excel / Word 能力被抽成独立的 MCP 服务（`mcp-office-server/`），后端以 stdio 子进程方式托管。
好处是工具契约标准化：每个工具都带 MCP 注解（`readOnlyHint` / `destructiveHint`），
Agent 侧不需要硬编码"哪些工具危险"，审批策略直接从注解推导。Server 本身完全无状态，可以脱离本项目单独复用。

**2. 审批放在 Agent 层，而不是 MCP 内部**

MCP Server 保持无状态，调用即写入。审批由后端在工具调用与真正执行之间拦一层：
提案落库 → 前端展示 Diff → 用户确认 → 才真正调用工具。这样审批记录是持久、可审计的，
不需要依赖任何图状态在服务重启后仍然存活。

写入前会自动备份原文件，所以即使 Agent 判断失误，也能一键还原。

**3. 检索用混合链路，而不是单路向量**

制度类文档的答案常常是精确数字（"3000 元""30 个自然日"），单靠向量会把这类 token 模糊掉；
而表格里的自然语言提问又需要语义泛化。所以：

```
向量召回（语义）  ┐
                  ├─▶ RRF 融合 ─▶ 重排（百炼 gte-rerank）─▶ Top-K 带出处
BM25 召回（精确） ┘
```

RRF 之所以必要：BM25 分数和余弦相似度不在同一量纲上，只有排序可比较。

**4. 检索精度与回答质量分开处理（父子分块）**

召回要的是精确，回答要的是上下文，这两件事靠一种块大小是调不好的：

- **子块**是被索引和被排序的单元——表格的一行、文档的一段，所以命中的引用能精确到
  "报销明细!第3行"，而不是含糊的"2-13 行区间"。
- **父块**是子块所处的整体（一个行组、一个标题章节），命中后附加给模型作为上下文，
  否则单看一行"金额=8600"是读不懂的。

父块不写入向量库，因此索引成本比"把两种粒度都嵌入"低一半。

Word 超长段落的切分按 **token** 计量而不是字符：`langchain-text-splitters`
用 bge-m3 自己的 tokenizer 预算（`CHUNK_SIZE_TOKENS=512`，块间重叠
`CHUNK_OVERLAP_TOKENS=64`），中文句读优先，句子不拦腰断；前导语切完再拼，
不占正文预算。切分粒度变更后需对存量工作区执行 reindex（见下文"迁移"）。

**5. 没有答案时要能说"没有"（相关性阈值）**

这是整条链路里最容易被忽略、但影响最大的一环：如果无论相关度多低都硬返回 top-k，
那么用户问一个知识库里根本不存在的问题时，模型仍然会拿到五段"最不无关"的文本，
然后编出一个听起来合理的答案。

链路的最后一环用交叉编码器的真实分数做过滤，低于阈值的候选直接丢弃；同时系统提示
要求模型在片段置信度普遍偏低时如实说明"没有找到"，而不是用常识补答。

**6. 回复逐字流出，且关掉了推理模型的思考阶段**

回复是**真流式**：后端在 agent 节点里用 `astream()` 消费模型输出，并通过 LangGraph 的
`stream_mode=["updates", "messages"]` 把每个 token 作为 SSE 的 `token` 事件推给前端，
前端累积渲染。工具调用与提案事件照常穿插在同一路流里。

这里有个容易踩的坑：**`qwen3.x-flash` 是推理模型**，它会先流式输出一大段
`reasoning_content`（思考过程），之后才开始输出答案。实测同一句话：

| | 首个**答案** token | 总耗时 |
|---|---|---|
| 默认（思考开启） | 8.6s | 8.9s |
| `LLM_ENABLE_THINKING=false` | **0.7s** | **1.0s** |

也就是说，开启思考时用户盯着转圈的那十几秒，模型其实一直在"想"，而这段内容
LangChain 并不会透出（`additional_kwargs` 为空），无法显示给用户。对"限额是多少"
"张伟报销了多少"这类事实型问题，这段推理带来的质量提升有限，所以默认关闭，
需要多步推理时把 `LLM_ENABLE_THINKING` 改成 `true`。

## 技术选型

| 层 | 选型 | 备注 |
|---|---|---|
| 前端 | Vue 3 + Vite + TypeScript + Pinia + Ant Design Vue | SSE 流式渲染工具调用过程 |
| 业务后端 | Java 21 + Spring Boot（backend-java/） | 对外 8000，REST/SSE 网关 + SQLite 独占 |
| AI 服务 | Python 3.11/3.12 + FastAPI（ai-service/，无状态） | 内网 8001，SSE 用 `sse-starlette` |
| Agent | LangGraph（单 Agent + 工具循环） | 迭代次数有上限，防止死循环 |
| LLM | 百炼 `qwen-plus`（OpenAI 兼容模式） | 改 `LLM_MODEL` 即可换模型 |
| Embedding | **本地** `BAAI/bge-m3`（1024 维） | 权重在本地，建索引不出网 |
| Reranker | **本地** `BAAI/bge-reranker-v2-m3` | 交叉编码器，本地推理 |
| 文档工具 | MCP Server（FastMCP + openpyxl + python-docx），stdio | 精选 22 个工具（7 只读 + 15 写入需审批） |
| 向量库 | Qdrant local（嵌入式） | 每个工作区一个 collection |
| 关系库 | SQLite（仅 backend-java 打开） | 工作区 / 文件 / 消息 / 操作审计 |

> LangGraph 只负责编排，不自带任何模型。LLM、embedding、rerank 分别由
> `app/llm/providers.py` 的工厂函数产出，换供应商只改配置。

**为什么只有 LLM 走云端**：对话生成需要的是通用推理能力，本地小模型在工具调用
（尤其是稳定的 JSON schema 遵循）上明显更差，而这恰好是 Agent 的成败关键。反过来，
embedding 和 rerank 是判模型不是生成模型，7B 以下的本地权重在中文检索任务上已经够用，
而且把整个知识库的原文留在本机，比省那点 token 费用重要得多。

## 快速开始

### 1. 准备模型凭证

在 [阿里云百炼](https://bailian.console.aliyun.com/) 申请 API Key，然后：

```bash
cp .env.example .env
# 编辑 .env，填入 DASHSCOPE_API_KEY=sk-xxxxxxxx
```

> 国际站账号把 `LLM_BASE_URL` 改为 `https://dashscope-intl.aliyuncs.com/compatible-mode/v1`。

### 2. 准备本地模型

Embedding 与 reranker 用本地权重，默认路径写在 `.env` 里：

```ini
EMBEDDING_PROVIDER=local
EMBEDDING_LOCAL_PATH=C:/code/agent/rag/models/bge-m3
RERANKER_PROVIDER=local-bge
RERANKER_LOCAL_PATH=C:/code/agent/rag/models/bge-reranker-v2-m3
```

模型没下载的话，用 HuggingFace CLI 拉到该目录：

```bash
pip install -U "huggingface_hub[cli]"
huggingface-cli download BAAI/bge-m3 --local-dir C:/code/agent/rag/models/bge-m3
huggingface-cli download BAAI/bge-reranker-v2-m3 --local-dir C:/code/agent/rag/models/bge-reranker-v2-m3
```

验证权重能被正确加载（会检查维度与重排效果）：

```bash
./.venv312/python.exe scripts/check_local_models.py
```

> `EMBEDDING_DIMENSIONS` 必须与模型实际输出维度一致。bge-m3 是 1024 维；
> 换成 `bge-large-zh-v1.5` 则需要改成 1024 也可以，但若是别的模型请先跑上面的脚本确认。

### 3. 启动后端（AI 服务 + Java 业务后端）

后端拆成两个进程：`ai-service/`（FastAPI，AI 能力 + MCP 子进程，端口 8001，仅内网）
和 `backend-java/`（Spring Boot，业务端点与 SQLite，端口 8000，唯一对前端）。

```bash
# Python AI 服务（推荐 Python 3.12；需要 JDK 21+ 跑 Java 侧）
conda create -p .venv312 python=3.12 -y
./.venv312/python.exe -m pip install -r ai-service/requirements.txt
./.venv312/python.exe -m pip install -e ./mcp-office-server

# 启动 AI 服务（Windows）
$env:PYTHONPATH = "ai-service"; ./.venv312/python.exe -m uvicorn app.main:app --reload --port 8001
# macOS / Linux
# PYTHONPATH=ai-service .venv312/bin/python -m uvicorn app.main:app --reload --port 8001

# 另开一个终端，启动 Java 业务后端（从仓库根目录跑，Maven wrapper 会自动拉 Maven）
cd backend-java && ./mvnw spring-boot:run   # Windows 用 mvnw.cmd
```

对外端点是 Java 后端 `http://127.0.0.1:8000`（健康检查 `/health`，会聚合
ai-service 状态）；AI 服务在 `http://127.0.0.1:8001` 只接受 Java 后端的内部调用。

### 4. 启动前端

```bash
cd frontend
npm install
npm run dev
```

打开 `http://localhost:5173`。前端通过 Vite 代理转发 `/api`，浏览器只面对一个源，SSE 不需要额外协商 CORS。

### 5. 用 Docker 启动（可选）

```bash
docker compose up --build
```

前端在 `http://localhost:5173`，后端在 `http://localhost:8000`。

> Docker 部署时注意：容器内读不到宿主机的模型目录。要么把模型目录挂载进去并在
> `.env` 里改成容器内路径，要么改用云端 embedding（`EMBEDDING_PROVIDER=dashscope`）。
> `docker-compose.yml` 已经预留了 `./data` 卷，模型目录按同样方式加一行即可。

## 演示流程

生成一份真实可用的演示数据——一份含超标记录的报销明细，一份规定了限额的报销制度：

```bash
python scripts/make_demo_data.py
```

然后在界面上：

1. 新建工作区，上传 `demo/` 下的两个文件（上传即自动建索引）
2. 提问"制度里规定的招待费单笔上限是多少？" → Agent 调用知识库检索，回答带出处
3. 提问"帮我找出报销明细里超过制度限额的记录" → Agent 先检索制度拿到限额，再读表格比对
4. 让 Agent 把这些记录的状态改成"需总经理审批" → 右侧出现 Diff 卡片，**确认前文件不会变动**
5. 点"应用修改" → 写入文件并自动备份，可随时还原

## 项目结构

```
├── mcp-office-server/        # 独立 MCP 服务：Excel/Word 工具 + 路径沙箱
│   ├── src/mcp_office_server/
│   │   ├── server.py         # MCP 工具定义（含注解，决定审批级别）
│   │   ├── sandbox.py        # 路径沙箱：拒绝绝对路径、穿越、符号链接逃逸
│   │   ├── excel_ops.py      # openpyxl 实现层（无 MCP 依赖，可单测）
│   │   ├── word_ops.py       # python-docx 实现层（含跨 run 替换）
│   │   └── errors.py         # 统一错误语义
│   └── tests/
├── ai-service/               # Python AI 服务（内网 8001）：agent / 检索 / 嵌入 / MCP 子进程
│   ├── app/
│   │   ├── agent/graph.py    # LangGraph 图 + 工具门控
│   │   ├── mcp_client.py     # MCP 子进程生命周期 + 注解解析
│   │   ├── llm/              # 模型 provider + 熔断/回退
│   │   └── retrieval/        # 分块 / BM25 / 向量库 / RRF 管线
│   └── tests/
├── backend-java/             # Java 业务后端（对外 8000）：REST/SSE 网关 + SQLite 独占
│   ├── src/main/java/…/      # 工作区/文件/消息/提案状态机、迁移、SSE 中继
│   └── src/test/java/…/      # JUnit5 + WireMock（桩掉 ai-service，不调模型）
├── frontend/src/             # Vue 三栏界面
└── scripts/                  # 演示数据、检索评测、冒烟脚本
```

## 测试

```bash
# 一键自检：拉起真实服务，确认 MCP 子进程与接口都正常
./.venv312/python.exe scripts/check_server.py

# 确认模型与本地权重可用（工具调用 + 查询改写 + 嵌入维度 + 重排排序）
./.venv312/python.exe scripts/check_llm.py
./.venv312/python.exe scripts/check_local_models.py
./.venv312/python.exe scripts/check_proxy_bypass.py

# 命令行提问（后端需已运行）
./.venv312/python.exe scripts/ask.py "销售表里 A型 的销售额是多少？"

# 打印原始 SSE 事件序列（排查流式/工具调用问题）
./.venv312/python.exe scripts/dump_sse.py "制度里规定的招待费上限是多少？"

# 全新工作区端到端流式验证（含工具调用、token 流、引用）
./.venv312/python.exe scripts/check_stream_e2e.py

# 工具调用决策校验：确认关掉思考后模型仍会检索而不是凭记忆作答
./.venv312/python.exe scripts/check_tool_decision.py

# 提案 → 待确认链路：确认 proposal 事件到达且后端确实存了一条 proposed 记录
./.venv312/python.exe scripts/check_proposal_flow.py

# 检查 SSE 原始字节的行尾（CRLF vs LF），用于排查事件丢失
./.venv312/python.exe scripts/inspect_sse_bytes.py

# 前端：SSE 帧解析回归 + 对接真实服务器的流式检查
cd frontend && npm run check:sse && npm run check:sse:live

# 真实 HTTP 端到端：起服务器 → 上传 → 提问 → 拒答，走完整 SSE 链路
./.venv312/python.exe scripts/check_chat_http.py

# MCP 服务：沙箱与文档读写
./.venv312/python.exe -m pytest mcp-office-server/tests -q

# AI 服务：检索、Agent 门控、内部端点（fake LLM，不调真实模型）
./.venv312/python.exe -m pytest ai-service/tests -q

# Java 业务后端：CRUD、SSE 中继与落库（WireMock 桩掉 ai-service）
cd backend-java && ./mvnw test    # Windows 用 mvnw.cmd

# 一键全跑（含上述三个套件）
./.venv312/python.exe scripts/run_all_tests.py

# 本地模型专项（需权重存在，会加载真实模型，较慢）
./.venv312/python.exe -m pytest ai-service/tests/test_local_providers.py -q -m slow
```

覆盖的关键场景：

- **路径沙箱**：`../` 穿越、绝对路径、NUL 注入、符号链接逃逸、多根配置
- **文档读写**：公式单元格与缓存值、区域截断、跨 run 文本替换、表格单元格越界
- **父子分块**：子块带表头且定位到行/段、父块覆盖其全部子块、表格行成为独立子块
- **混合检索**：跨通道去重保留首次出现位置、未知 id 被丢弃、指纹忽略空白差异
- **相关性门控**：低于阈值全部过滤后返回空、阈值 0 时不生效、无重排分数时不误卡
- **查询改写**：改写结果同时作用于向量与关键词两路、对话历史传入改写器、关闭时零模型调用、失败与异常输出回退原查询
- **审批闭环**：提案不落盘、拒绝后文件字节不变、应用前备份、重复应用被拒、还原生效
- **Agent 门控**：只读工具直通、写入工具被拦截、提案携带 Diff、未知工具不崩溃、迭代上限生效
- **本地模型**：维度与配置一致、语义相近文本余弦相似度更高、重排把依据条款排到第一位
- **端到端**：建工作区 → 上传 Excel+Word → 索引 → 工具清单 → 删除

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| `GET/POST/DELETE` | `/api/workspaces[/{id}]` | 工作区增删查 |
| `GET/POST/DELETE` | `/api/workspaces/{id}/files[/{fid}]` | 文件上传（上传即建索引）与删除 |
| `POST` | `/api/workspaces/{id}/files/{fid}/reindex` | 重建单个文件的索引 |
| `POST` | `/api/workspaces/{id}/reindex` | **重建整个工作区**，用于分块策略或模型变更后的迁移 |
| `POST` | `/api/workspaces/{id}/chat/stream` | 对话，SSE 事件 `token / tool_call / tool_result / proposal / citations / done / error` |
| `POST` | `/api/workspaces/{id}/operations/{op}/apply|reject|revert` | 审批闭环 |
| `GET` | `/api/workspaces/{id}/tools` | MCP 工具清单及其审批级别 |

Agent 的工具选择测试用脚本化的假模型驱动，因此**不需要 API key** 也能验证控制流。

## 检索效果评测

`scripts/eval_retrieval.py` 内置 36 条针对演示语料的标注问题，分四类：库内事实（18）、
口语化提问（6）、**库外负样本（8）**、多轮指代（4）。负样本是这个评测集的关键——
它们检验的是"能不能正确拒答"，而一个没有阈值过滤的链路在这项上是必然不及格的。

```bash
python scripts/eval_retrieval.py                      # 仅 BM25，不需要 API key
python scripts/eval_retrieval.py --dense              # + 本地向量与重排
python scripts/eval_retrieval.py --dense --rewrite    # + LLM 查询改写（需 key）
python scripts/eval_retrieval.py --dense --sweep      # 阈值扫描
```

实测结果（同一评测集，36 题）：

| 配置 | Recall@5 | MRR | 拒答准确率 |
|---|---|---|---|
| 仅 BM25（无模型、无 key） | 0.8214 | 0.8036 | 0.2500 |
| + 向量 + 重排 + 阈值 0.02 | 0.8929 | 0.8571 | 1.0000 |
| **+ 查询改写（完整链路）** | **0.9286** | **0.9107** | **1.0000** |

按类别的分布：

| 类别 | 仅 BM25 | + 向量重排 | + 查询改写 |
|---|---|---|---|
| 库内事实 (18) | 18/18 | 18/18 | 18/18 |
| 口语化提问 (6) | 2/6 | 4/6 | 5/6 |
| 库外负样本 (8) | 2/8 | **8/8** | **8/8** |
| 多轮指代 (4) | 3/4 | 2/4 | **3/4** |

两个数字值得单独说：

**拒答从 0.25 提到 1.00。** 仅 BM25 时链路无条件返回 top-k，所以 8 个库外问题里有 6 个
拿到了不该给的片段——模型会照此编答案。加上阈值后全部正确拒答。

**多轮指代必须靠改写。** 这组问题（"那第三条呢""它的上限是多少"）本身不带任何可检索
的语义。注意中间那一列是 **2/4，比仅 BM25 的 3/4 还低**——这不是退步，而是阈值在
起作用：无法解析的问题被拦成"拒答"，而不是返回一段不相关的内容给模型。开启改写后
恢复到 3/4，因为"那第三条呢"能被补全成"报销制度第三条的规定"。

改写的实际输出（`scripts/check_llm.py` 会打印）：

| 原问题 | 改写结果 |
|---|---|
| 那第三条呢 | 报销制度第三条的规定 |
| 它的上限是多少 | 招待费上限是多少 |
| 报销咋整 | 报销流程 |
| 谁还没给批啊 | 未审批人员 |
| 请客吃饭能报几个钱 | 业务招待费报销标准 |

改写触发率 0.7222（26/36）——已经清晰的问题不会被改动。

**剩余两个未命中**：`谁还没给批啊` 被改写成"未审批人员"，但表格里的字段值是"待审批"，
属于同义词未被覆盖；另一条多轮问题在实测中遇到改写请求超时、回退成了原查询。
后者已通过把 `QUERY_REWRITE_TIMEOUT` 从 15s 提到 45s（实测改写耗时 4~8s，原来的
上限没有留出余量）修复；修复后的完整评测记录见 `eval-rewrite.json`
（36 题：库内 18/18、口语化 5/6、负样本 8/8、多轮 3/4，改写触发率 0.7222）。

### 阈值怎么标定

阈值不是抄来的，是扫出来的。`--sweep` 复用同一批检索结果、只改变过滤条件，
因此扫描本身几乎不增加开销：

| 阈值 | Recall@5 | 拒答准确率 |
|---|---|---|
| 0（不卡） | 1.0000 | 0.0000 |
| 0.005 | 0.9643 | 0.6250 |
| 0.010 | 0.9286 | 0.8750 |
| **0.020** | **0.8929** | **1.0000** |
| 0.050 | 0.8929 | 1.0000 |
| 0.120 | 0.7857 | 1.0000 |

0.02 是第一个把拒答做到 100% 的取值，再往上只会损失召回。这就是默认值的来历。

> **换语料或换重排模型后必须重新扫描。** 阈值取决于被打分文本的长度：长段落上
> 正确命中能到 0.9，而检索实际打分的短子块只有 0.05 量级。照搬别的项目的阈值会
> 静默损伤召回。

### 性能

- **一次 36 题完整评测**：不开启改写 117 秒，开启改写约 380 秒（CPU）——差值就是
  36 次改写 LLM 调用的开销。
- **单次查询**：重排约 0.8 秒（20 个候选），改写 4~8 秒。
- 评测曾经耗时 **341 秒**（未开启改写时），差别来自一个真实缺陷：重排模型原来每次检索
  都重新加载一次，加载约 10 秒，36 次检索就是 360 秒。按配置缓存重排器后降到 117 秒。
- 端到端对话实测：一次问答约 **10~18 秒**。作为对照，修复提示词的工具有问题之前，
  同一个问题因为 Agent 连续调用了 6 次工具，耗时 **136 秒**。

分块从"12 行一块"改为父子结构后，索引的嵌入量也减少一半——父块不参与嵌入。

## 配置项

全部在 `.env` 中，详见 `.env.example`：

| 变量 | 默认 | 说明 |
|---|---|---|
| `DASHSCOPE_API_KEY` | — | 必填，供 LLM 使用（embedding/rerank 走本地时不参与） |
| `LLM_MODEL` | `qwen-plus` | 可换 `qwen-max` 等 |
| `LLM_ENABLE_THINKING` | `false` | 推理模型的思考阶段。开启会使首个答案 token 从 ~0.7s 推迟到 ~8.6s |
| `EMBEDDING_PROVIDER` | `local` | `local` 走本地权重；`dashscope` 走云端 |
| `EMBEDDING_LOCAL_PATH` | `C:/code/agent/rag/models/bge-m3` | 本地权重目录 |
| `EMBEDDING_DEVICE` | `cpu` | 装了 CUDA 版 torch 可改 `cuda` |
| `EMBEDDING_DIMENSIONS` | `1024` | 必须与模型实际输出维度一致 |
| `RERANKER_PROVIDER` | `local-bge` | `local-bge` / `dashscope` / `noop` |
| `RERANKER_LOCAL_PATH` | `C:/code/agent/rag/models/bge-reranker-v2-m3` | 本地权重目录 |
| `RERANK_TOP_N` | `5` | 最终返回片段数 |
| `RERANK_SCORE_THRESHOLD` | `0.02` | 相关性下限，低于此分丢弃；`0` 关闭。**换语料需重新扫描** |
| `QUERY_REWRITE_ENABLED` | `true` | 检索前用 LLM 规范查询、补全多轮指代 |
| `QUERY_REWRITE_MODEL` | `qwen-turbo` | 改写专用模型，独立于对话模型 |
| `QUERY_REWRITE_HISTORY_TURNS` | `4` | 改写时参考的历史轮数 |
| `QUERY_REWRITE_TIMEOUT` | `45` | 改写超时；实测耗时 4~8s，上限留足余量 |
| `AGENT_MAX_ITERATIONS` | `12` | 工具循环上限 |

切换回云端 embedding / rerank 只需改两个变量（需 `DASHSCOPE_API_KEY`）：

```ini
EMBEDDING_PROVIDER=dashscope
RERANKER_PROVIDER=dashscope
```

把 `RERANKER_PROVIDER` 设为 `noop` 则跳过重排，只用 RRF 融合结果——零模型加载，启动最快。

## 已知限制

- 单用户本地运行，没有账号体系与多租户隔离。
- 文档修改限定在结构化编辑（值、区域、段落、表格、基础格式）。
  上游实现里的图表、透视表、文档保护等能力保留在代码中但未注册为工具——工具过多会明显拉低模型的工具选择准确率。
- 只有对话与查询改写需要 `DASHSCOPE_API_KEY`；嵌入与重排走本地权重。未配置 key 时
  关键词检索仍可用，但向量链路与查询改写会自动降级（不阻断检索）。
- 多轮指代问题（"那第三条呢"）依赖查询改写解析。改写关闭时它们会被阈值正确拦下并
  返回空结果——这是刻意行为，不是缺陷，但意味着关闭改写会牺牲多轮场景。
- 修改分块策略或更换嵌入模型后，已有工作区需要调用
  `POST /api/workspaces/{id}/reindex` 重建索引。启动时若检测到旧格式分块会打印告警
  并给出该接口，但不会自动重建，以免阻塞启动。

## 排障

**Windows 系统代理会拦掉本地请求。** 如果这台机器上开了全局代理（注册表
`Internet Settings\ProxyEnable`），而用的 HTTP 客户端会读取系统代理设置，那么发往
`127.0.0.1` 的请求可能被转发给代理并返回 **502 Bad Gateway**——看起来像服务端崩了，
实际服务端完全正常，日志里也没有任何异常。症状是具有欺骗性的：小请求（如 `/health`）
能过，稍大的 POST 就失败。

诊断方法：用 `httpx.Client(trust_env=False)` 绕开代理重试。仓库里的验收脚本都已显式
关闭代理读取。

**Schema 变更不会自动作用于已有数据库。** `Base.metadata.create_all` 只创建**缺失的表**，
从不修改已存在的表。所以给模型加字段后，用全新数据库跑的测试全绿，而真实数据库会在
写入时报 `no such column`。`app/migrations.py` 负责在启动时补上新增的列与索引，
策略是**只增不删**（不加列、不建表、不改数据），因此可以安全地每次启动自动执行。

**不要给 `a-empty` 传 `:image="null"`。** ant-design-vue 的 Empty 组件内部有
`typeof image === "object" && "type" in image`，而 `typeof null === "object"` 成立，
于是执行 `"type" in null` 抛 TypeError。因为异常发生在渲染阶段，Vue 会中断挂载，
页面表现为**完全空白**；又因为堆栈指向 `node_modules/.vite/deps/ant-design-vue.js`，
错误信息看起来像依赖或缓存问题，很容易把人引向错误的方向（清缓存、重装依赖都无效）。

要自定义空状态就直接用自己的 `div`，本项目所有空状态都是这么做的。

页面启动失败时不会静默白屏：`index.html` 里有一段诊断脚本，会把异常原文和修复提示
渲染到页面上，直接看页面提示比翻控制台更快。

**模型调用必须绕过系统代理。** httpx 会从环境变量**以及** Windows 注册表读取代理配置，
所以进程启动时代理是开着的，它就会在**整个生命周期内**都往那个代理发请求。用户之后把
代理关掉，进程仍会去连一个已经没有服务在监听的本地端口，报出：

```
httpx2.ConnectError: [WinError 10061] 由于目标计算机积极拒绝，无法连接
→ openai.APIConnectionError: Connection error
```

这个故障的误导性在于：从任何**新**进程发起同样的请求都会成功（代理已关闭、配置干净），
只有那个常驻服务在失败，很容易让人以为是模型厂商侧的问题。

`app/llm/providers.py` 因此显式构造 `trust_env=False` 的 httpx 客户端并传给
`ChatOpenAI`（必须是 `openai.DefaultHttpxClient` 的子类，SDK 会做类型检查）。
本项目只访问云端模型端点和本地 MCP 子进程，走代理只会带来故障。

`scripts/check_proxy_bypass.py` 会把环境变量指向一个死端口来验证这一点：

```bash
./.venv312/python.exe scripts/check_proxy_bypass.py
```

**流式回调里不要用推送前的局部变量引用改状态。** 对话的流式事件（工具调用、引用、
最终文本）都发生在 `push` 之后。如果回调里改的是推送前捕获的那个局部对象，写操作会落到
原始对象上而不是 Vue 的响应式代理上，界面不会更新——症状是**回复要切换到别的会话再切回来
才出现**，因为重新读取数据才触发渲染。

正确做法是通过响应式数组按 id 查找后再改（见 store 里的 `turnById`）。
`frontend/reactivity-check.cjs` 用 `@vue/reactivity` 的 `effect` 复现了这两种写法：

```bash
cd frontend && node reactivity-check.cjs

# pre-push reference -> render effect sees ""            (renders 2 -> 2)   改了不渲染
# lookup by id       -> render effect sees "已修改 C3"    (renders 4 -> 5)   正确渲染
```

**SSE 帧用 CRLF 结尾，不能按 `'\n\n'` 切分。** `sse-starlette` 发出的帧是：

```
event: token\r\ndata: {...}\r\n\r\n
```

而字节序列 `\r\n\r\n` **不包含** `\n\n`（它是 `\r`、`\n`、`\r`、`\n`）。所以用
`buffer.indexOf('\n\n')` 找帧边界永远匹配不上：所有事件都堆在缓冲区里，直到连接关闭才
走末尾的兜底分支——而那个分支把**整段多帧文本当成一个块**解析，`event` 取最后一个、
`data` 行全部拼接，`JSON.parse` 必然失败，于是返回 `{event, data: {raw}}`。

症状是事件"凭空消失"（工具调用、提案都收不到），且文本只在流结束时一次性出现。
修法是**按行解析**并同时兼容 CRLF/LF，见 `frontend/src/composables/sse.ts`。

> 这类 bug 有个特别坑的地方：**后端测试全都是通过的**。因为 httpx 的 `iter_lines()`
> 会归一化所有行尾，只有浏览器里手写的解析器才暴露问题。所以流式相关的验收必须有一条
> 走真实浏览器（或等价的原始字节解析）的检查。

```bash
cd frontend
npm run check:sse        # 帧解析回归测试（含 CRLF 与跨包分片）
npm run check:sse:live   # 用真实服务器的 SSE 流验证，含 proposal 事件
```
- Windows 上创建符号链接需要额外权限，相关沙箱测试会自动跳过。

## 许可与致谢

本项目 MIT 许可。文档工具层的设计借鉴了两个 MIT 许可的开源项目，均非直接复制代码，
来源与借鉴范围见 [`mcp-office-server/NOTICE.md`](mcp-office-server/NOTICE.md)：

- [haris-musa/excel-mcp-server](https://github.com/haris-musa/excel-mcp-server) — FastMCP + openpyxl 分层、路径包含性校验
- [GongRzhe/Office-Word-MCP-Server](https://github.com/GongRzhe/Office-Word-MCP-Server) — Word 能力的模块化组织方式
