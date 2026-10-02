"""索引吞吐基准：上传 N 份文件并计时到全部 indexed（第二阶段性能验收用）。

用法（后端与中间件需已启动，本地 bge-m3 嵌入，不调 LLM）：
  ./.venv312/python.exe scripts/bench_upload.py [--count 10] [--label baseline]

流程：生成 N 份 demo 文件拷贝 → 一个 multipart 请求上传（立即返回 indexing 快照）
→ 每 0.5s 轮询文件列表，直到全部离开 indexing/pending → 打印分段耗时。

纯本地基准：目标固定为 BACKEND（127.0.0.1 回环，与 scripts/ 其他冒烟脚本同约定）。
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx

REPO = Path(__file__).resolve().parent.parent
BACKEND = "http://127.0.0.1:8000"


def make_files(count: int, target: Path) -> list[Path]:
    target.mkdir(parents=True, exist_ok=True)
    src = REPO / "demo" / "2026年第一季度报销明细.xlsx"
    files = []
    for i in range(count):
        dest = target / f"bench-{i:02d}-报销明细.xlsx"
        dest.write_bytes(src.read_bytes())
        files.append(dest)
    return files


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--label", default="run")
    args = parser.parse_args()

    bench_dir = REPO / "data" / "bench-upload"
    files = make_files(args.count, bench_dir)

    with httpx.Client(base_url=BACKEND, timeout=120.0) as client:
        ws = client.post(
            "/api/workspaces",
            json={"name": f"bench-{args.label}-{int(time.time())}"},
        ).raise_for_status().json()["id"]

        t0 = time.perf_counter()
        client.post(
            f"/api/workspaces/{ws}/files",
            files=[("files", (f.name, f.read_bytes())) for f in files],
        ).raise_for_status()
        t_upload = time.perf_counter() - t0

        rows: list[dict] = []
        while True:
            rows = client.get(f"/api/workspaces/{ws}/files").raise_for_status().json()
            pending = [r for r in rows if r["status"] in ("indexing", "pending")]
            if not pending:
                break
            time.sleep(0.5)
            if time.perf_counter() - t0 > 900:
                print("TIMEOUT after 900s")
                break
        t_total = time.perf_counter() - t0

    chunks = sum(r["chunk_count"] for r in rows)
    failed = [r for r in rows if r["status"] == "failed"]
    print(json.dumps({
        "label": args.label,
        "files": len(rows),
        "chunks": chunks,
        "failed": len(failed),
        "upload_s": round(t_upload, 2),
        "total_s": round(t_total, 2),
        "per_file_s": round(t_total / max(len(rows), 1), 2),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
