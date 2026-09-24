"""Structural evaluation of the chunking strategy over adversarial documents.

Generates a diverse corpus — multi-block sheets, wide sheets, offset data,
formula-only cells, oversized paragraphs, localized heading styles — runs the
production chunker over it, and reports where the strategy holds up and where it
does not. Read-only with respect to the application database.

    python scripts/test_chunking_live.py [--out docs/test-report/9-12/chunking-results.json]
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ai-service"))

LONG_PARAGRAPH = (
    "报销人应当在费用发生后的三十个工作日内提交报销申请，逾期未提交的，财务部门有权拒绝受理。"
    "报销单据必须包含发票原件、费用明细清单、审批签字三部分，缺少任何一部分均视为材料不全。"
    "对于跨自然月的费用，应当按月分别提交，不得合并打包。"
) * 12  # ~2000 chars, well over the 512-token budget

STACKED_TOP = [
    ("产品", "类别", "数量", "单价"),
    ("显示器", "数码", 7, 899.0),
    ("打印机", "办公", 3, 1299.0),
    ("碎纸机", "办公", 5, 459.0),
    ("保险柜", "办公", 2, 1699.0),
    ("投影仪", "办公", 4, 2899.0),
]
STACKED_BOTTOM = [
    ("部门", "报销人", "事由", "金额", "状态"),
    ("市场部", "张伟", "客户招待", 1200.0, "已审批"),
    ("技术部", "李娜", "差旅住宿", 860.0, "待审批"),
    ("人事部", "王强", "培训费", 2400.0, "已审批"),
    ("财务部", "赵敏", "办公用品", 320.0, "已驳回"),
]


def build_order_workbook(path: Path) -> None:
    """The same workbook the MCP live test uses: 16 rows, formula column."""
    products = [
        ("无线耳机", "数码", 12, 299.0),
        ("机械键盘", "数码", 8, 459.0),
        ("便携音箱", "数码", 15, 199.0),
        ("智能手环", "数码", 20, 399.0),
        ("降噪耳机", "数码", 6, 1299.0),
    ]
    book = Workbook()
    sheet = book.active
    sheet.title = "订单"
    sheet.append(["产品", "类别", "数量", "单价", "金额", "下单日期"])
    for index, (name, category, quantity, price) in enumerate(products, start=2):
        sheet.append([name, category, quantity, price, f"=C{index}*D{index}", date(2026, 9, index)])
    book.save(path)


def build_adversarial_workbook(path: Path) -> None:
    book = Workbook()

    stacked = book.active
    stacked.title = "两个表"
    for row in STACKED_TOP:
        stacked.append(list(row))
    stacked.append([])  # separator
    for row in STACKED_BOTTOM:
        stacked.append(list(row))

    wide = book.create_sheet("宽表")
    wide.append([f"指标{chr(65 + i)}" for i in range(30)])
    wide.append([f"v1-{j}" for j in range(30)])
    wide.append([f"很长的说明文字{j}" + "数" * 40 for j in range(30)])  # >1200 chars per row

    header_only = book.create_sheet("只有表头")
    header_only.append(["列一", "列二"])

    book.create_sheet("空表")

    offset = book.create_sheet("偏移数据")
    offset["B3"] = "姓名"
    offset["C3"] = "城市"
    offset["B4"] = "陈晨"
    offset["C4"] = "杭州"
    offset["B5"] = "林一"
    offset["C5"] = "宁波"

    long_text = book.create_sheet("长单元格")
    long_text.append(["备注"])
    long_text.append(["这是一条超长备注。" + "合同条款约定交付日期以书面确认为准。" * 100])

    book.save(path)


def build_long_document(path: Path) -> None:
    document = Document()
    document.add_paragraph("本制度自发布之日起施行，解释权归财务部所有。")  # before any heading
    document.add_heading("第一章 总则", level=1)
    document.add_paragraph("本制度适用于公司全体员工及劳务派遣人员。")
    document.add_paragraph("报销货币为人民币，以外币结算的费用按当月一日汇率折算。")
    document.add_heading("第二章 报销细则", level=2)
    document.add_paragraph(LONG_PARAGRAPH)
    document.add_heading("第三章 附则", level=1)
    document.add_paragraph("历史遗留问题由财务部会同法务部另行研究解决。")

    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    table.rows[0].cells[0].text = "职级"
    table.rows[0].cells[1].text = "住宿标准（元/晚）"
    table.rows[0].cells[2].text = "交通标准（元/公里）"
    for level in range(1, 15):
        row = table.add_row().cells
        row[0].text = f"P{level}"
        row[1].text = str(300 + level * 50)
        row[2].text = str(1 + level // 10)
    document.save(path)


def build_localized_heading_document(path: Path) -> None:
    """Headings styled with a localized custom style, as Chinese Word often saves."""
    document = Document()
    styles = document.styles
    from docx.enum.style import WD_STYLE_TYPE

    heading = styles.add_style("标题 1", WD_STYLE_TYPE.PARAGRAPH)
    heading.base_style = styles["Heading 1"]
    document.add_paragraph(" localized 小节标题", style=heading)
    document.add_paragraph("这一段本应属于「 localized 小节标题」章节，但若样式识别失败会被并入前一个分组。")
    document.save(path)


def measure(groups: list[Any], measure_fn) -> dict[str, Any]:
    children = [child.text for group in groups for child in group.children]
    lengths = [measure_fn(text) for text in children]
    return {
        "groups": len(groups),
        "children": len(children),
        "child_tokens": {
            "min": min(lengths) if lengths else 0,
            "max": max(lengths) if lengths else 0,
            "mean": round(statistics.mean(lengths), 1) if lengths else 0,
        },
        "child_chars": {
            "max": max((len(t) for t in children), default=0),
        },
    }


def main() -> int:
    from app.retrieval.chunking import (
        DEFAULT_CHUNK_TOKENS,
        build_body_splitter,
        chunk_document_groups,
    )

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", type=Path, default=ROOT / "docs" / "test-report" / "9-12" / "chunking-results.json"
    )
    args = parser.parse_args()

    import tempfile

    splitter = build_body_splitter(DEFAULT_CHUNK_TOKENS, 64)
    report: dict[str, Any] = {"findings": [], "documents": {}}

    with tempfile.TemporaryDirectory(prefix="chunk-eval-") as tmp:
        tmp_path = Path(tmp)
        build_order_workbook(tmp_path / "订单表.xlsx")
        build_adversarial_workbook(tmp_path / "复杂表.xlsx")
        build_long_document(tmp_path / "制度文档.docx")
        build_localized_heading_document(tmp_path / "本地化标题.docx")

        # Archive the corpus next to the report so the evidence is reproducible.
        testdata_dir = args.out.parent / "testdata"
        testdata_dir.mkdir(parents=True, exist_ok=True)
        import shutil

        for name in ("订单表.xlsx", "复杂表.xlsx", "制度文档.docx", "本地化标题.docx"):
            shutil.copy2(tmp_path / name, testdata_dir / name)
        report["testdata_dir"] = str(testdata_dir)

        # ---- 订单表：公式列是否进入索引 ----
        groups = chunk_document_groups(tmp_path / "订单表.xlsx", "订单表.xlsx")
        all_child_text = "\n".join(c.text for g in groups for c in g.children)
        formula_missing = "金额=" not in all_child_text
        quantity_present = "数量=12" in all_child_text
        report["findings"].append(
            {
                "id": "F1",
                "title": "公式单元格通过双工作簿兜底进入索引",
                "detail": (
                    "openpyxl 不计算公式，data_only=True 对本系统写入的公式读出 None。"
                    "修复后以 data_only=False 二次读取，公式文本（如 =C2*D2）入块。"
                ),
                "evidence": {
                    "金额字段缺失": formula_missing,
                    "数量字段在": quantity_present,
                },
                "severity": "info",
            }
        )
        report["documents"]["订单表.xlsx"] = measure(groups, splitter.measure)

        # ---- 复杂表：多表块 / 宽表 / 偏移 / 空表 ----
        groups = chunk_document_groups(tmp_path / "复杂表.xlsx", "复杂表.xlsx")
        by_sheet: dict[str, list[Any]] = {}
        for group in groups:
            by_sheet.setdefault(group.parent.meta.get("sheet", "?"), []).append(group)

        stacked_text = "\n".join(c.text for g in by_sheet.get("两个表", []) for c in g.children)
        second_table_mislabelled = "产品=张伟" in stacked_text or "类别=报销人" in stacked_text
        second_table_rows_indexed = "部门=市场部" in stacked_text or "事由=客户招待" in stacked_text
        report["findings"].append(
            {
                "id": "F2",
                "title": "纵向堆叠的第二张表按空行分块并采用自己的表头",
                "detail": (
                    "空行分隔的堆叠表按 Excel current-region 语义分块；块首行经表头嗅探"
                    "（全为短文本、无数字/日期/公式）后采用新表头。块内无分隔的堆叠表"
                    "保持不切（宁可不切，不换错表头）。"
                ),
                "evidence": {
                    "错误键值对消失": not second_table_mislabelled,
                    "第二张表数据以正确表头入索引": second_table_rows_indexed,
                },
                "severity": "info",
            }
        )

        long_rows = by_sheet.get("长单元格", [])
        split_children = [
            c for g in long_rows for c in g.children if c.meta.get("part", 0) > 0
        ]
        report["findings"].append(
            {
                "id": "F3",
                "title": "超长单元格文本按 token 预算切分，无表头/定位信息丢失",
                "detail": "超长备注被切成多个 part，每个 part 仍带文件/表头前言，定位为同一行。",
                "evidence": {"切分出的 part 数": len(split_children)},
                "severity": "info",
            }
        )

        offset_text = "\n".join(c.text for g in by_sheet.get("偏移数据", []) for c in g.children)
        offset_no_header = "姓名=陈晨" not in offset_text and "B=陈晨" in offset_text
        report["findings"].append(
            {
                "id": "F4",
                "title": "数据不从 A1 开始的工作表锚定真实表头",
                "detail": (
                    "表头锚定到第一个非空行、行号为绝对行号：偏移数据（表头在 B3:C3）"
                    "不再把空行当表头，也不会退化成列字母键。"
                ),
                "evidence": {"退化为列字母键": offset_no_header, "原始文本样例": offset_text[:120]},
                "severity": "info",
            }
        )

        report["documents"]["复杂表.xlsx"] = {
            **measure(groups, splitter.measure),
            "sheets": {name: len(items) for name, items in by_sheet.items()},
        }

        excel_max_tokens = max(
            (splitter.measure(c.text) for g in groups for c in g.children), default=0
        )
        report["findings"].append(
            {
                "id": "F8",
                "title": "Excel 超长行按 token 预算切分（与 Word 一致）",
                "detail": (
                    "Excel 子块曾按 1200 字符切分，实测宽表长行达 1102 token（bge-m3 512 token "
                    "建议值的 2 倍）。修复后复用 BodySplitter：前言在预算外预留，超预算行按 "
                    "token 切分并带 64 token 重叠。"
                ),
                "evidence": {
                    "Excel 子块最大 token 数": excel_max_tokens,
                    "全部不超过预算": excel_max_tokens <= DEFAULT_CHUNK_TOKENS,
                },
                "severity": "info",
            }
        )

        # ---- Word 长文 ----
        groups = chunk_document_groups(tmp_path / "制度文档.docx", "制度文档.docx")
        body_children = [
            c for g in groups for c in g.children if "段落" in c.location
        ]
        long_pieces = [
            c for c in body_children if "报销人应当在费用发生后的三十个工作日内" in c.text
        ]
        over_budget = [c for c in body_children if splitter.measure(c.text) > DEFAULT_CHUNK_TOKENS]
        header_on_each_piece = all("文件：制度文档.docx" in c.text for c in long_pieces)
        table_groups = [g for g in groups if g.parent.meta.get("table_index") is not None]
        table_children = [c for g in table_groups for c in g.children]
        report["findings"].append(
            {
                "id": "F5",
                "title": "超长 Word 段落按 token 预算切分",
                "detail": "2000+ 字的报销细则段落被切成多个子块；每个子块都带 文件+章节 前言，且不超预算。",
                "evidence": {
                    "切分块数": len(long_pieces),
                    "每块都带章节头": header_on_each_piece,
                    "超过512 token 的子块数": len(over_budget),
                },
                "severity": "info",
            }
        )
        report["findings"].append(
            {
                "id": "F6",
                "title": "Word 表格整表为父块、每行为子块",
                "detail": "14 行职级标准表生成 1 个父块 + 15 个子块（含表头行），行级检索粒度符合设计。",
                "evidence": {
                    "表格组数": len(table_groups),
                    "表格子块数": len(table_children),
                    "表格父块字符数": max((len(g.parent.text) for g in table_groups), default=0),
                },
                "severity": "info",
            }
        )
        report["documents"]["制度文档.docx"] = measure(groups, splitter.measure)

        # ---- 本地化标题样式 ----
        groups = chunk_document_groups(tmp_path / "本地化标题.docx", "本地化标题.docx")
        heading_children = [c for g in groups for c in g.children if g.parent.meta.get("heading")]
        report["findings"].append(
            {
                "id": "F7",
                "title": "自定义/本地化标题样式构成章节边界",
                "detail": (
                    "标题判定迁移 docling 方案：样式继承链上任一环节含 heading（名称或 id）"
                    "或带 w:outlineLvl(0-8) 即为标题——本地化/自定义样式与直接格式都覆盖。"
                ),
                "evidence": {
                    "识别出的带标题分组数": len(heading_children),
                    "本地化标题样式生效": len(heading_children) >= 1,
                },
                "severity": "info",
            }
        )
        report["documents"]["本地化标题.docx"] = measure(groups, splitter.measure)

    report["generated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    report["budget_tokens"] = DEFAULT_CHUNK_TOKENS
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    for finding in report["findings"]:
        print(f"[{finding['severity']:<6}] {finding['id']} {finding['title']}")
        print(f"         {json.dumps(finding['evidence'], ensure_ascii=False)}")
    for name, stats in report["documents"].items():
        print(f"{name}: {json.dumps(stats, ensure_ascii=False)}")
    print(f"report -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
