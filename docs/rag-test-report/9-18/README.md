# RAG 权威基准测评（第三轮）· 2026-09-18

本目录是第三轮 RAG 测评的全部产出。与前两轮（CMRC 2018 + 业务语料、
BM25 离线扫参，见 [../](../README.md)）的分工：

| 轮次 | 回答的问题 | 手段 |
| --- | --- | --- |
| 第一/二轮 | 分块参数与分块策略是否最优 | BM25 离线网格扫描，零模型调用 |
| **本轮（第三轮）** | **整套混合检索管线在权威基准上是否站得住；检索各环节各值多少；答案层传导如何** | **CRUD-RAG + C-MTEB 标准基准，真实模型全量推理（本地 bge-m3 / bge-reranker-v2-m3；qwen3.8-flash 试点后配额耗尽）** |

## 结论速览

| 问题 | 答案 |
| --- | --- |
| 现行架构在权威基准上的水平 | **CRUD-RAG 单跳子集 hit@5 全线 100%（16 组合全饱和）**；CMTEB T2Reranking 上融合优于任一单腿、重排 MAP/NDCG/MRR 三项全场最高 |
| 分块 512/64 要不要改 | **不改**——top-5 口径四档无差异；更小块成本 ×5.2 零收益；CRUD-RAG 论文"单文档偏小块"在我们的检索口径 + 父子架构下不成立（报告中如实对照） |
| RRF 权重 1.0/0.8 要不要调 | **不调**——四组权重逐位零差异 |
| 重排候选数 20 要不要加大 | **不加**——50 与 20 完全一致，本地 CPU 延迟省 2.5 倍；候选生成 top-30 已覆盖 99.65% 正相关 |
| RAGFlow"自动关键词"附注 | **实测无效，不上线**——关键词来自块内已索引文本，BM25 零差异（负结果留档） |
| 重排会不会伤召回 | **会，且可接受**——R@10 0.7875 略低于融合 0.7949：重排用尾部广度换头部精度，而生产只读头部（top-5），广度由融合层兜底 |
| 检索优化传导到答案吗 | **检索饱和时不传导**——试点 16 题 ROUGE-L 逐位相同（两模式上下文一致）；与 CRUD-RAG 官方"重排对单文档 QA 提升很小"互证。答案质量的杠杆在生成端 |

## 文件

| 测评 | 通俗版 | 面试版 | 数据 |
| --- | --- | --- | --- |
| CRUD-RAG 检索与分块（3000 新闻 + 86 题，四档分块 × 四种检索模式 + 关键词 A/B + 等价性校验） | [通俗版](./CRUD-RAG检索分块测评-通俗版.md) | [面试版](./CRUD-RAG检索分块测评-面试版.md) | [data/crud-rag-eval.json](./data/crud-rag-eval.json) · [data/keyword-ab.json](./data/keyword-ab.json) |
| C-MTEB T2Reranking 向量检索与重排（500 题官方候选池，MAP/NDCG@10/MRR@10/R@10） | [通俗版](./向量检索与重排CMTEB测评-通俗版.md) | [面试版](./向量检索与重排CMTEB测评-面试版.md) | [data/cmteb-rerank-eval.json](./data/cmteb-rerank-eval.json) |
| 端到端回答质量（ROUGE-L，试点 16/86 题，云端配额中断） | [通俗版](./端到端回答质量测评-通俗版.md) | [面试版](./端到端回答质量测评-面试版.md) | 复跑即出（见面试版 §5） |

## 测评资产（本轮新增）

- `scripts/eval_crud_rag.py`——CRUD-RAG 全管线测评（分块扫描 / 四模式 /
  RRF 权重 / 候选数 / 等价性校验；嵌入按配置缓存，重跑只算增量）；
- `scripts/eval_rerank_cmteb.py`——C-MTEB T2Reranking 官方候选池协议测评；
- `scripts/eval_keyword_annotation.py`——RAGFlow 式关键词附注 A/B（BM25 腿）；
- `scripts/eval_crud_answers.py`——端到端回答质量（ROUGE-L，配额恢复后复跑）；
- `tests/datasets/`——CRUD-RAG / C-MTEB 子集标注与池结构（[README](../../../tests/datasets/README.md)），
  原始大语料按 `.gitignore` 不入库、可从官方渠道重建。

## 如何复现

```bash
python scripts/eval_crud_rag.py --out docs/rag-test-report/9-18/data/crud-rag-eval.json
python scripts/eval_rerank_cmteb.py --out docs/rag-test-report/9-18/data/cmteb-rerank-eval.json
python scripts/eval_keyword_annotation.py --out docs/rag-test-report/9-18/data/keyword-ab.json
# 配额恢复后：
python scripts/eval_crud_answers.py --out docs/rag-test-report/9-18/data/crud-answers-eval.json
```

前两项为本地真实模型推理（首次运行需数小时 CPU 时间，之后命中嵌入缓存）；
数据集就位方式见 `tests/datasets/README.md`。

## 已知限制（如实）

- CRUD-RAG 用的是官方子集（86 题），±1 题 = 1.2 个百分点，报告内所有
  小差异均标注噪声级；多跳/多文档子集未测；
- CMTEB 嵌入按 512 token 截断（与生产子块预算、重排器截断对齐），与
  榜单数字量级可比、不逐位可比；
- 端到端仅完成试点（云端账号"仅免费模式"配额耗尽，涉及全部 qwen
  模型与云端 rerank；本地推理不受影响）；
- 本地重排在 CPU 上约 3 对/秒，生产如需扩候选数或换更大重排器，需先
  解决推理吞吐（GPU/ONNX/云端付费三选一）。
