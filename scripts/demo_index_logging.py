"""演示/验收索引链路文件日志：不经 Java 后端直接驱动 ai-service。

Docker/RabbitMQ 未启动时，backend-java 的上传链路（需要 MQ）在本机跑不起来；
本脚本按 Java 上传的落盘方式（文件直接进 ``data_dir/workspaces/<ws>/``，等价于
tests/conftest.seed_workspace_file）准备文件，然后调用 IndexingConsumer 攒批
消费时会用的同一个端点 ``POST /v1/index-batch``。ai-service 产生的日志与
全链路中完全一致：``<data_dir>/logs/ai-service.log``（逐 chunk 明细）。

用法（ai-service 需已启动，--data-dir 必须与服务进程的 DATA_DIR 一致）：
  python scripts/demo_index_logging.py [--base-url http://127.0.0.1:8099] \
      [--data-dir <DATA_DIR>] [--workspace demo-log-ws]
"""

from __future__ import annotations

import argparse
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent


def seed(data_dir: Path, workspace: str) -> list[dict]:
    """把 demo 语料拷进工作区目录（Java 上传的落盘半边），返回索引请求清单。"""
    ws_dir = data_dir / "workspaces" / workspace
    ws_dir.mkdir(parents=True, exist_ok=True)

    seeded: list[Path] = []
    for name in ("2026年第一季度报销明细.xlsx", "员工费用报销管理制度.docx"):
        src = REPO / "demo" / name
        if src.exists():
            (ws_dir / name).write_bytes(src.read_bytes())
            seeded.append(src)
    if not seeded:
        raise SystemExit(f"no demo corpus found under {REPO / 'demo'}")

    return [
        {"file_id": f"demo-{index}", "rel_path": path.name}
        for index, path in enumerate(seeded)
    ]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8099")
    parser.add_argument("--data-dir", default=str(REPO / "data"))
    parser.add_argument("--workspace", default="demo-log-ws")
    args = parser.parse_args()

    files = seed(Path(args.data_dir), args.workspace)
    print(f"seeded {len(files)} file(s) into {args.data_dir}\\workspaces\\{args.workspace}:")
    for item in files:
        print(f"  - {item['rel_path']}")

    with httpx.Client(base_url=args.base_url, timeout=300.0) as client:
        response = client.post(
            "/v1/index-batch",
            json={"workspace_id": args.workspace, "files": files},
        )
        response.raise_for_status()
        for item, result in zip(files, response.json()):
            print(
                f"{result['status']:8} {item['rel_path']}: "
                f"chunks={result['chunk_count']} vectors={result['vector_count']} "
                f"error={result['error']}"
            )

    print(f"\nlog file: {Path(args.data_dir) / 'logs' / 'ai-service.log'}")


if __name__ == "__main__":
    main()
