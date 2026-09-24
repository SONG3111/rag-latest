"""Live, real-model test of every Excel MCP tool. Writes a JSON report.

For each spreadsheet tool this script drives the full agent stack — the real LLM
selected by ``build_chat_model`` (see .env), the real MCP server subprocess over
stdio, the proposal/approval loop — and then verifies the resulting file bytes
with openpyxl. What is *not* real: the knowledge-base search returns empty
results (the files in this throwaway workspace are never embedded; the corpus
loader hands the retriever an empty corpus), and approval is automatic instead
of human (each proposal is applied by invoking its tool directly, the same call
backend-java makes through /v1/tools/call after approving).

    python scripts/test_excel_mcp_live.py [--out docs/test-report/9-12/excel-mcp-live-results.json]

Exit code 0 when every scenario passes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai-service"))

PRODUCTS: list[tuple[str, str, int, float]] = [
    ("无线耳机", "数码", 12, 299.0),
    ("机械键盘", "数码", 8, 459.0),
    ("便携音箱", "数码", 15, 199.0),
    ("智能手环", "数码", 20, 399.0),
    ("降噪耳机", "数码", 6, 1299.0),
    ("蓝牙鼠标", "数码", 25, 89.0),
    ("USB-C 扩展坞", "配件", 18, 259.0),
    ("显示器支架", "配件", 10, 169.0),
    ("笔记本支架", "配件", 14, 129.0),
    ("移动电源", "配件", 30, 149.0),
    ("桌面麦克风", "数码", 5, 699.0),
    ("摄像头", "数码", 9, 349.0),
    ("键盘膜", "配件", 40, 19.0),
    ("鼠标垫", "配件", 50, 29.0),
    ("读卡器", "配件", 22, 59.0),
    ("HDMI 线", "配件", 35, 39.0),
]

SCENARIO_TIMEOUT = 300.0


def build_workbook(path: Path) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "订单"
    sheet.append(["产品", "类别", "数量", "单价", "金额", "下单日期"])
    for index, (name, category, quantity, price) in enumerate(PRODUCTS, start=2):
        sheet.append([name, category, quantity, price, f"=C{index}*D{index}", date(2026, 9, index)])
    summary = book.create_sheet("汇总")
    summary.append(["指标", "数值"])
    summary.append(["产品种类", "=COUNTA(订单!A2:A17)"])
    summary.append(["目标销售额", 10000])
    summary.append(["平均单价", 318])
    book.save(path)


async def drive_agent(agent, question: str) -> dict[str, Any]:
    """Run one turn and collect a compact trace of everything that happened."""
    from app.agent.graph import AgentEvent

    trace: dict[str, Any] = {
        "question": question,
        "tool_calls": [],
        "tool_results": [],
        "proposals": [],
        "final": "",
        "error": None,
    }
    started = time.time()
    try:
        async with asyncio.timeout(SCENARIO_TIMEOUT):
            async for event in agent.astream(question):
                if event.type == "tool_call":
                    trace["tool_calls"].append(
                        {"tool": event.data.get("tool"), "args": event.data.get("args")}
                    )
                elif event.type == "tool_result":
                    content = event.data.get("content", "")
                    trace["tool_results"].append(json.loads(content) if content else {})
                elif event.type == "proposal":
                    trace["proposals"].append(event.data)
                elif event.type == "error":
                    trace["error"] = event.data.get("message")
                elif event.type == "done":
                    trace["final"] = event.data.get("content", "")
    except (TimeoutError, Exception) as exc:  # noqa: BLE001 - record and continue
        trace["error"] = f"{type(exc).__name__}: {exc}"
    trace["seconds"] = round(time.time() - started, 1)
    return trace


def called_tools(trace: dict[str, Any]) -> list[str]:
    return [call["tool"] for call in trace["tool_calls"]]


def check(condition: bool, failures: list[str], message: str) -> None:
    if not condition:
        failures.append(message)


def verify_read_cells(path: Path, failures: list[str]) -> None:
    """Order sheet: 16 products + header, formula column, date column intact."""
    book = load_workbook(path)
    sheet = book["订单"]
    check(sheet.max_row == 17, failures, f"订单 max_row={sheet.max_row}, expected 17")
    check(sheet["A2"].value == "无线耳机", failures, f"A2={sheet['A2'].value!r}")
    check(sheet["D2"].value == 299.0, failures, f"D2={sheet['D2'].value!r}")
    check(
        isinstance(sheet["F2"].value, date) and sheet["F2"].value == date(2026, 9, 2),
        failures,
        f"F2={sheet['F2'].value!r}",
    )


# --------------------------------------------------------------------------- #
# scenarios
# --------------------------------------------------------------------------- #
def scenarios(xlsx: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "S01",
            "tool": "list_files",
            "question": "工作区里现在有哪些文件？分别是什么类型？",
            "expect_tools": ["list_files"],
            "verify_trace": lambda trace, failures: check(
                any(
                    f.get("path") == xlsx
                    for result in trace["tool_results"]
                    if result.get("ok")
                    for f in (result.get("data", {}).get("files") or [])
                ),
                failures,
                "list_files 结果里没有看到目标文件",
            ),
        },
        {
            "id": "S02",
            "tool": "get_doc_structure",
            "question": "看一下订单表.xlsx 的整体结构：有哪些工作表，各有多少行、什么表头？",
            "expect_tools": ["get_doc_structure"],
            "verify_trace": lambda trace, failures: check(
                any(
                    sheet.get("name") == "订单" and sheet.get("max_row") == 17
                    for result in trace["tool_results"]
                    if result.get("ok")
                    for sheet in (result.get("data", {}).get("sheets") or [])
                ),
                failures,
                "结构里没有 sheet=订单 max_row=17",
            ),
        },
        {
            "id": "S03",
            "tool": "read_range",
            "question": "把订单表.xlsx「订单」工作表的数据原样列出来。",
            "expect_tools": ["read_range"],
            "verify_trace": lambda trace, failures: check(
                any(
                    isinstance(row[0], str) and row[0] == "产品"
                    for result in trace["tool_results"]
                    if result.get("ok") and result.get("data", {}).get("sheet") == "订单"
                    for row in (result.get("data", {}).get("values") or [])
                ),
                failures,
                "read_range 结果没有返回表头行",
            ),
        },
        {
            "id": "S04",
            "tool": "read_range",
            "question": "订单表.xlsx「订单」工作表 A1:C4 区域的内容是什么？",
            "expect_tools": ["read_range"],
            "verify_trace": lambda trace, failures: check(
                any(
                    result.get("data", {}).get("end_cell") == "C4"
                    and (result.get("data", {}).get("values") or [[None]])[0][0] == "产品"
                    for result in trace["tool_results"]
                    if result.get("ok")
                ),
                failures,
                "read_range 未按 A1:C4 返回",
            ),
        },
        {
            "id": "S05",
            "tool": "find_text",
            "question": "在订单表.xlsx 里查找「无线耳机」出现在哪些位置。",
            "expect_tools": ["find_text"],
            "verify_trace": lambda trace, failures: check(
                any(
                    hit.get("cell") == "A2"
                    for result in trace["tool_results"]
                    if result.get("ok") and "hits" in (result.get("data") or {})
                    for hit in (result.get("data", {}).get("hits") or [])
                ),
                failures,
                "find_text 没有命中 A2",
            ),
        },
        {
            "id": "S06",
            "tool": "find_text",
            "question": "订单表.xlsx 里哪些地方出现了 299 这个值？",
            "expect_tools": ["find_text"],
            "note": "回归：find_text 曾只匹配字符串单元格，数值 299 会被漏报为“没有找到”",
            "verify_trace": lambda trace, failures: check(
                any(
                    hit.get("cell") == "D2"
                    for result in trace["tool_results"]
                    if result.get("ok") and "hits" in (result.get("data") or {})
                    for hit in (result.get("data", {}).get("hits") or [])
                ),
                failures,
                "find_text('299') 未命中数值单元格 D2",
            ),
        },
        {
            "id": "S07",
            "tool": "update_cells",
            "question": "把订单表.xlsx「订单」工作表里「无线耳机」那一行的数量改为 50。",
            "expect_tools": ["update_cells"],
            "verify_file": lambda path, failures: check(
                load_workbook(path)["订单"]["C2"].value == 50,
                failures,
                "应用后 C2 != 50",
            ),
        },
        {
            "id": "S08",
            "tool": "update_cells",
            "question": "把订单表.xlsx「汇总」工作表的 B3 改成 120，B4 改成 88.5。",
            "expect_tools": ["update_cells"],
            "verify_file": lambda path, failures: (
                check(load_workbook(path)["汇总"]["B3"].value == 120, failures, "B3 != 120"),
                check(load_workbook(path)["汇总"]["B4"].value == 88.5, failures, "B4 != 88.5"),
            ),
        },
        {
            "id": "S09",
            "tool": "set_formula",
            "question": "在订单表.xlsx「汇总」工作表的 B5 单元格写入公式：计算 B3 与 B4 的和。",
            "expect_tools": ["set_formula"],
            "verify_file": lambda path, failures: check(
                str(load_workbook(path)["汇总"]["B5"].value or "").startswith(("=B3+B4", "=B4+B3", "=SUM"))
                or (
                    str(load_workbook(path)["汇总"]["B5"].value or "").startswith("=")
                    and "B3" in str(load_workbook(path)["汇总"]["B5"].value)
                    and "B4" in str(load_workbook(path)["汇总"]["B5"].value)
                ),
                failures,
                f"B5 不是引用 B3、B4 的求和公式: {load_workbook(path)['汇总']['B5'].value!r}",
            ),
        },
        {
            "id": "S10",
            "tool": "insert_rows",
            "question": "在订单表.xlsx「订单」工作表第 4 行的位置前面插入 2 行空行。",
            "expect_tools": ["insert_rows"],
            "verify_file": lambda path, failures: (
                check(
                    load_workbook(path)["订单"].max_row == 19,
                    failures,
                    f"max_row={load_workbook(path)['订单'].max_row}, expected 19",
                ),
                check(
                    load_workbook(path)["订单"]["A4"].value is None
                    and load_workbook(path)["订单"]["A5"].value is None,
                    failures,
                    "第4、5行应为空行",
                ),
                check(
                    load_workbook(path)["订单"]["A6"].value == "便携音箱",
                    failures,
                    f"A6={load_workbook(path)['订单']['A6'].value!r}, expected 便携音箱(原第4行下移)",
                ),
            ),
        },
        {
            "id": "S11",
            "tool": "delete_rows",
            "question": "删除订单表.xlsx「订单」工作表的第 6 行和第 7 行这两行。",
            "expect_tools": ["delete_rows"],
            "verify_file": lambda path, failures: (
                check(
                    load_workbook(path)["订单"].max_row == 17,
                    failures,
                    f"max_row={load_workbook(path)['订单'].max_row}, expected 17",
                ),
                check(
                    load_workbook(path)["订单"]["A6"].value == "降噪耳机",
                    failures,
                    f"A6={load_workbook(path)['订单']['A6'].value!r}, expected 降噪耳机(原第8行上移)",
                ),
            ),
        },
        {
            "id": "S12",
            "tool": "format_range",
            "question": "把订单表.xlsx「订单」工作表的表头行 A1:F1 加粗，并给这些单元格填充颜色 FFD966。",
            "expect_tools": ["format_range"],
            "verify_file": lambda path, failures: (
                check(
                    load_workbook(path)["订单"]["A1"].font.bold is True,
                    failures,
                    "A1 未加粗",
                ),
                check(
                    (load_workbook(path)["订单"]["A1"].fill.start_color.rgb or "").endswith("FFD966"),
                    failures,
                    f"A1 填充色={load_workbook(path)['订单']['A1'].fill.start_color.rgb}",
                ),
            ),
        },
        {
            "id": "S13",
            "tool": "format_range",
            "question": "把订单表.xlsx「汇总」工作表 B2:B4 的数字格式设置为千分位格式 #,##0。",
            "expect_tools": ["format_range"],
            "verify_file": lambda path, failures: check(
                load_workbook(path)["汇总"]["B3"].number_format == "#,##0",
                failures,
                f"B3 number_format={load_workbook(path)['汇总']['B3'].number_format!r}",
            ),
        },
    ]


async def run_direct_error_checks(client, workspace_id: str) -> list[dict[str, Any]]:
    """Implementation-level error paths, exercised straight through the MCP session."""
    from app.mcp_client import parse_tool_result

    target = f"{workspace_id}/订单表.xlsx"
    checks: list[dict[str, Any]] = []

    async def invoke(name: str, arguments: dict[str, Any]):
        tool = client.tool(name)
        return parse_tool_result(await tool.ainvoke(arguments))

    # Stale digest must be rejected before any write happens.
    payload = await invoke(
        "update_cells",
        {
            "path": target,
            "sheet_name": "订单",
            "updates": [{"cell": "C2", "value": 1}],
            "expected_digest": "0" * 64,
        },
    )
    checks.append(
        {
            "check": "过期 digest 被拒绝",
            "ok": payload.get("ok") is False,
            "detail": payload.get("error"),
        }
    )
    # Unknown sheet surfaces a structured error, not a crash.
    payload = await invoke("read_range", {"path": target, "sheet_name": "不存在"})
    checks.append(
        {
            "check": "不存在的工作表返回结构化错误",
            "ok": payload.get("ok") is False and payload.get("error", {}).get("code") == "sheet_not_found",
            "detail": payload.get("error"),
        }
    )
    # Row indexes are 1-based.
    payload = await invoke("delete_rows", {"path": target, "sheet_name": "订单", "start_row": 0, "count": 1})
    checks.append(
        {
            "check": "delete_rows start_row=0 被拒绝",
            "ok": payload.get("ok") is False,
            "detail": payload.get("error"),
        }
    )
    return checks


async def main_async(out_path: Path) -> int:
    import tempfile

    from app.agent.graph import AgentRuntime, WorkspaceAgent
    from app.config import get_settings
    from app.llm.providers import build_chat_model
    from app.mcp_client import McpOfficeClient, parse_tool_result

    settings = get_settings()
    if not settings.dashscope_api_key:
        print("DASHSCOPE_API_KEY 未配置，无法做真实模型测试")
        return 2

    with tempfile.TemporaryDirectory(prefix="mcp-live-") as tmp:
        tmp_path = Path(tmp)
        settings.data_dir = tmp_path / "data"
        settings.ensure_directories()

        # 无状态模式：工作区只是一个目录名，没有数据库行；agent 拿到的
        # runtime 就是 /v1/chat/stream 会为 Java 构造的那份（空语料 + 在册文件）。
        workspace_id = "ws-excel-live"
        ws_dir = settings.workspace_dir(workspace_id)
        ws_dir.mkdir(parents=True, exist_ok=True)
        build_workbook(ws_dir / "订单表.xlsx")

        results: list[dict[str, Any]] = []
        client = McpOfficeClient(workspace_root=settings.workspaces_dir)
        await client.start()
        try:
            print(f"model={settings.llm_model}  tools={len(client.tools())}")
            llm = build_chat_model(settings)

            direct = await run_direct_error_checks(client, workspace_id)
            for item in direct:
                print(f"  [{'OK ' if item['ok'] else 'FAIL'}] {item['check']}: {item['detail']}")
            results.append({"id": "S00", "tool": "(direct error paths)", "checks": direct})

            for scenario in scenarios("订单表.xlsx"):
                print(f"\n== {scenario['id']} [{scenario['tool']}] {scenario['question']}")
                runtime = AgentRuntime(
                    workspace_id=workspace_id,
                    tracked_files={"订单表.xlsx"},
                    corpus_loader=lambda: ({}, []),
                )
                agent = WorkspaceAgent(client, settings, llm=llm, runtime=runtime)
                trace = await drive_agent(agent, scenario["question"])
                record: dict[str, Any] = {
                    "id": scenario["id"],
                    "tool": scenario["tool"],
                    "question": scenario["question"],
                    "seconds": trace["seconds"],
                    "tool_calls": trace["tool_calls"],
                    "error": trace["error"],
                    "final": trace["final"][:400],
                }
                failures: list[str] = []
                if trace["error"]:
                    failures.append(f"agent error: {trace['error']}")

                tools = called_tools(trace)
                expected = scenario.get("expect_tools") or []
                for name in expected:
                    check(name in tools, failures, f"模型未调用 {name}（实际: {tools}）")
                for result in trace["tool_results"]:
                    if result.get("ok") is False:
                        failures.append(f"工具返回错误: {result.get('error')}")

                # Writes: apply every pending proposal this turn produced, then judge
                # the file itself. The proposal must exist before the file changes.
                # (Auto-approval = invoking the proposed tool call directly, exactly
                # what backend-java does through /v1/tools/call after approving.)
                applied: list[dict[str, Any]] = []
                proposals = trace["proposals"]
                for proposal in proposals:
                    target = ws_dir / "订单表.xlsx"
                    before = target.read_bytes()
                    try:
                        tool = client.tool(proposal["tool"])
                        result_payload = parse_tool_result(
                            await tool.ainvoke(proposal["arguments"])
                        )
                        applied.append(
                            {
                                "tool": proposal["tool"],
                                "summary": proposal["summary"],
                                "result": result_payload,
                                "file_changed": target.read_bytes() != before,
                            }
                        )
                    except Exception as exc:  # noqa: BLE001
                        failures.append(f"apply 失败: {exc}")
                record["proposals"] = [
                    {"tool": p["tool"], "summary": p["summary"], "diff": p.get("diff")}
                    for p in proposals
                ]
                record["applied"] = applied

                if "verify_trace" in scenario:
                    scenario["verify_trace"](trace, failures)
                if "verify_file" in scenario:
                    if not proposals:
                        failures.append("没有产生任何修改提案")
                    scenario["verify_file"](ws_dir / "订单表.xlsx", failures)

                if scenario.get("note"):
                    record["note"] = scenario["note"]
                record["verdict"] = "PASS" if not failures else "FAIL"
                record["failures"] = failures
                results.append(record)
                print(
                    f"  tools={tools} proposals={len(proposals)} "
                    f"-> {record['verdict']} {failures or ''}"
                )
        finally:
            await client.stop()

    passed = sum(1 for r in results if r.get("verdict") == "PASS")
    failed = sum(1 for r in results if r.get("verdict") == "FAIL")
    direct_ok = sum(1 for c in results[0]["checks"] if c["ok"]) if results and "checks" in results[0] else 0
    summary = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "model": settings.llm_model,
        "scenarios_passed": passed,
        "scenarios_failed": failed,
        "direct_checks_passed": direct_ok,
        "results": results,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\n{passed} passed, {failed} failed, direct {direct_ok}/{len(results[0]['checks']) if results and 'checks' in results[0] else 0}")
    print(f"results -> {out_path}")
    return 0 if failed == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out",
        type=Path,
        default=ROOT / "docs" / "test-report" / "9-12" / "excel-mcp-live-results.json",
    )
    args = parser.parse_args()
    return asyncio.run(main_async(args.out))


if __name__ == "__main__":
    raise SystemExit(main())
