"""Deterministic statistics pinned against a hand-computed workbook.

Every expected number below is worked out from the fixture by hand rather than
recorded from the implementation, so a regression in the arithmetic fails the
test instead of silently redefining the answer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from openpyxl import Workbook

from mcp_office_server import stats_ops
from mcp_office_server.errors import ColumnNotFound, InvalidValue


@pytest.fixture()
def stats_book(workspace: Path) -> Path:
    """A sales sheet with blanks, a text value and a formula with no cached result.

    区域     销售额
    华东     100, 200, 400          -> sum 700, count 3
    华北     300, (空), 500          -> sum 800, count 3
    华南     "待定", =SUM(1,2)       -> no readable number
    """
    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    sheet.append(["产品", "区域", "销售额", "备注"])
    sheet.append(["A型", "华东", 100, "首批"])
    sheet.append(["B型", "华东", 200, None])
    sheet.append(["C型", "华北", 300, "补单"])
    sheet.append(["D型", "华北", None, None])
    sheet.append(["E型", "华东", 400, None])
    sheet.append(["F型", "华南", "待定", None])
    sheet.append(["G型", "华南", "=SUM(1,2)", None])
    sheet.append(["H型", "华北", 500, None])
    path = workspace / "销售明细.xlsx"
    book.save(path)
    return path


# --------------------------------------------------------------------------- #
# describe_column
# --------------------------------------------------------------------------- #
def test_describe_column_by_header_name(stats_book: Path) -> None:
    payload = stats_ops.describe_column(stats_book, "销售", "销售额")

    assert payload["column"] == "销售额"
    assert payload["column_letter"] == "C"
    assert payload["count"] == 5
    assert payload["missing"] == 1  # D型 的空单元格
    assert payload["formula_without_cache"] == 1  # G型 的公式没有缓存结果
    assert payload["non_numeric_samples"] == ["待定"]
    assert payload["sum"] == 1500
    assert payload["mean"] == 300
    assert payload["median"] == 300
    assert payload["min"] == 100
    assert payload["max"] == 500
    assert payload["stdev"] == pytest.approx(158.11388300841897)
    assert payload["q1"] == 200  # 线性插值：(5-1)*0.25 = 1 -> ordered[1]
    assert payload["q3"] == 400
    assert "公式" in payload["hint"]


def test_describe_column_accepts_letter_and_number_references(stats_book: Path) -> None:
    by_letter = stats_ops.describe_column(stats_book, "销售", "C")
    by_number = stats_ops.describe_column(stats_book, "销售", "3")
    assert by_letter["sum"] == by_number["sum"] == 1500
    assert by_letter["column"] == "C"


def test_describe_column_reports_unknown_column(stats_book: Path) -> None:
    with pytest.raises(ColumnNotFound) as excinfo:
        stats_ops.describe_column(stats_book, "销售", "不存在列")
    assert "销售额" in str(excinfo.value)  # 可用列名列在错误里，便于模型自我纠正


def test_describe_column_rejects_a_column_with_no_numbers(stats_book: Path) -> None:
    with pytest.raises(InvalidValue):
        stats_ops.describe_column(stats_book, "销售", "备注")


# --------------------------------------------------------------------------- #
# aggregate
# --------------------------------------------------------------------------- #
def test_aggregate_sums_a_value_column_by_group(stats_book: Path) -> None:
    payload = stats_ops.aggregate(stats_book, "销售", "区域", value_column="销售额", agg="sum")

    assert payload["group_by"] == "区域"
    assert payload["value_column"] == "销售额"
    assert payload["group_count"] == 3
    assert payload["truncated"] is False
    assert payload["total_value"] == 1500
    # 数值分组按降序排在前面，无法聚合的分组（华南）恒定排在最后
    assert [(item["key"], item["value"]) for item in payload["groups"]] == [
        ("华北", 800),
        ("华东", 700),
        ("华南", None),
    ]
    assert payload["groups"][0]["count"] == 3  # 华北 3 行（含空值行）


def test_aggregate_median_and_ascending_order(stats_book: Path) -> None:
    payload = stats_ops.aggregate(
        stats_book, "销售", "区域", value_column="销售额", agg="median", sort="asc"
    )
    ranked = [
        (item["key"], item["value"])
        for item in payload["groups"]
        if item["value"] is not None
    ]
    assert ranked == [("华东", 200), ("华北", 400)]


def test_aggregate_without_a_value_column_counts_rows(stats_book: Path) -> None:
    payload = stats_ops.aggregate(stats_book, "销售", "区域")

    assert payload["agg"] == "count"
    assert payload["value_column"] is None
    assert {item["key"]: item["value"] for item in payload["groups"]} == {
        "华东": 3,
        "华北": 3,
        "华南": 2,
    }


def test_aggregate_truncates_to_top_n(stats_book: Path) -> None:
    payload = stats_ops.aggregate(
        stats_book, "销售", "区域", value_column="销售额", agg="sum", top_n=2
    )
    assert payload["truncated"] is True
    assert payload["group_count"] == 3
    assert len(payload["groups"]) == 2


def test_aggregate_counts_blank_groups_as_skipped(workspace: Path) -> None:
    book = Workbook()
    sheet = book.active
    sheet.title = "销售"
    sheet.append(["区域", "销售额"])
    sheet.append(["华东", 100])
    sheet.append([None, 200])
    path = workspace / "空分组.xlsx"
    book.save(path)

    payload = stats_ops.aggregate(path, "销售", "区域", value_column="销售额", agg="sum")
    assert payload["groups"] == [{"key": "华东", "value": 100.0, "count": 1}]
    assert payload["skipped"]["blank_group"] == 1


def test_aggregate_reports_unknown_aggregation(stats_book: Path) -> None:
    with pytest.raises(InvalidValue):
        stats_ops.aggregate(stats_book, "销售", "区域", value_column="销售额", agg="average")


def test_aggregate_reports_unknown_column(stats_book: Path) -> None:
    with pytest.raises(ColumnNotFound):
        stats_ops.aggregate(stats_book, "销售", "未知分组", value_column="销售额")


# --------------------------------------------------------------------------- #
# sort_by
# --------------------------------------------------------------------------- #
def test_sort_by_returns_the_numeric_top_rows(stats_book: Path) -> None:
    payload = stats_ops.sort_by(stats_book, "销售", "销售额", descending=True, top_n=3)

    assert payload["order_by"] == "销售额"
    assert payload["truncated"] is True
    # 8 个数据行 - 2 个空排序值（D型 空单元格 + G型 无缓存结果的公式）
    assert payload["row_count"] == 6
    assert [(row["rank"], row["产品"], row["销售额"]) for row in payload["rows"]] == [
        (1, "H型", 500),
        (2, "E型", 400),
        (3, "C型", 300),
    ]
    assert payload["skipped"] == {"blank_order_value": 2, "non_numeric_order_value": 1}


def test_sort_by_ascending_and_column_selection(stats_book: Path) -> None:
    payload = stats_ops.sort_by(
        stats_book, "销售", "销售额", descending=False, top_n=2, columns=["产品"]
    )
    assert [row["销售额"] for row in payload["rows"]] == [100, 200]
    # 只选了「产品」列，但排序列会自动补进输出，才能看懂排序依据
    assert set(payload["rows"][0]) == {"rank", "产品", "销售额"}


def test_sort_by_excludes_blank_and_counts_non_numeric(stats_book: Path) -> None:
    payload = stats_ops.sort_by(stats_book, "销售", "销售额", top_n=20)
    order = [row["产品"] for row in payload["rows"]]
    # 数值行按降序在前，文本行（待定）恒定排在数值之后
    assert order[:5] == ["H型", "E型", "C型", "B型", "A型"]
    assert order[5] == "F型"
    assert "D型" not in order
