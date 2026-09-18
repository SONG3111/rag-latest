# 评测数据集

本目录存放 RAG 测评用的公开数据集（已做预处理）。原始语料体积较大且可从
官方渠道重新获取，因此**不入库**（见 `.gitignore`）；标注与池结构文件入库，
保证测评可复现。

## crud_rag/ — CRUD-RAG Read 单跳问答子集

- 来源：[IAAR-Shanghai/CRUD_RAG](https://github.com/IAAR-Shanghai/CRUD_RAG)
  （arXiv:2401.17043），官方 Read 任务为 2023 年后中文新闻单跳 QA。
- 本子集：**3,000 篇新闻文档**（`docs/`，中位 707 字符，p90 925，最大 151k）+
  **86 条标注问题**（`evidence.jsonl`，含 `query / answer / relevant_chunks`，
  relevant_chunks 形如 `qa_0000_news1.txt#0`，指向回答该问题的唯一文档）。
- 预处理口径见 `prepare_stats.json`（从官方 qa 采样 100 条、剔除改写歧义 14 条、
  答案切分损坏 0 条，留 86 条；干扰文档 3,000 篇）。
- 使用脚本：`scripts/eval_crud_rag.py`（检索/分块）、`scripts/eval_crud_answers.py`（端到端回答）。

## cmteb_reranking/ — C-MTEB T2Reranking（dev）

- 来源：[T2Ranking](https://arxiv.org/abs/2304.03679)，C-MTEB Reranking 榜单
  背后的任务（MTEB 任务定义 `mteb/tasks/reranking/zho/cmteb_reranking.py`，
  主指标 MAP，另报 NDCG@10 / MRR@10）。
- 本子集：**500 条查询**（`evidence.jsonl`，含 `relevant_pids`）+ **8,208 段落**
  （`corpus.jsonl`，全部查询候选池的并集）+ **官方候选池**（`pools.jsonl`，
  从官方 dev parquet 的 positive/negative 列表逐条重建，文本精确匹配到 pid，
  0 条未匹配）。
- `corpus.jsonl`（约 8MB）与 `_raw/`（官方 parquet）不入库；`pools.jsonl` 入库，
  拿到 `corpus.jsonl` 后即可完整复现。
- 使用脚本：`scripts/eval_rerank_cmteb.py`。

## business_eval/ — 业务仿真集（30 条）

- 由本项目早期业务文档测评准备（`evidence.jsonl`），块标注指向当时工作区的
  分块文件；仅作参考留档，本轮测评未使用。
