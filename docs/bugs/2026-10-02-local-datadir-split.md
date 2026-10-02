# BUG-006：本地裸跑时 Java 与 ai-service 的 DATA_DIR 分叉，上传索引必失败

- 日期：2026-10-02（本地三服务冒烟时发现）
- 模块：README 本地启动指引 / backend-java 的 `rag.data-dir` 默认值
- 状态：已定位（冒烟时用 `DATA_DIR=../data` 规避）；README 补充说明

## 现象

按 README 组合本地裸跑：ai-service 从仓库根启动（`PYTHONPATH=ai-service uvicorn`），
backend-java 按 README 在 `backend-java/` 目录下 `./mvnw spring-boot:run`。
上传任何文件，队列链路本身正常（入队→消费→调 /v1/index），但 ai-service 报：

```
file is missing from disk
```

文件状态机最终 failed。文件确实落盘了——落在 `backend-java/data/workspaces/...`，
而 ai-service 在 `data/workspaces/...`（仓库根）找。

## 根因

`rag.data-dir` 默认 `${DATA_DIR:./data}` 是**相对进程工作目录**解析的：

- ai-service 按指引从仓库根跑 → `./data` = 仓库根 `data/`（历史数据都在这）；
- backend-java 按指引 `cd backend-java` 后跑 → `./data` = `backend-java/data/`。

两个进程各自持有"正确的" data 目录，但不是同一个。Docker 编排里
`DATA_DIR=/app/data` 两侧一致，所以全容器部署不复现——只有本地裸跑组合会踩。

## 修复 / 规避

本地裸跑 backend-java 时显式指回仓库根：

```bash
cd backend-java && DATA_DIR=../data ./mvnw spring-boot:run
```

（README 已按此更新启动命令。）

## 预防 / 教训

- 多进程共享目录的配置，默认值不要用相对路径——分叉时没有任何报错，
  直到下游进程"file is missing"才显形，且错误文案指向文件而根因在配置。
- 长期正解是把默认值改成"仓库根"语义（如 `./data` 相对仓库根跑 Java，
  或默认值指向 `${user.dir}/../data`），涉及历史数据路径迁移，本次只做
  README 纠偏 + 此记录留档。
