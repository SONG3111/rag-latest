"""insert_rows 带 values 的纯函数回归：合成 diff 与摘要措辞。

中段"插行+填数"现在是一次 insert_rows 带 values 的原子提案（MCP 侧
excel_ops.insert_rows 的 values 参数，见该处 docstring）。本文件钉住两处
配套行为，两者都是纯算术、不发网络读、不碰真实模型：

- graph.WorkspaceAgent._synthetic_insert_diff：提案卡的合成 diff
  （before 恒 None、values 从 A 列起按行给坐标、{"formula": ...} 解包）；
- services.operations.summarize_operation：摘要带"并写入 M 行数据"，
  与 backend-java 的 OperationSummaries 逐字同步，两个时点的卡片文案
  才不会漂移。
"""

from __future__ import annotations

from app.agent.graph import WorkspaceAgent, _normalize_insert_values
from app.services.operations import summarize_operation


def test_normalize_insert_values_parses_json_string():
    args = {"start_row": 5, "values": "[[\"鼠标垫\", 60]]"}
    _normalize_insert_values(args)
    assert args["values"] == [["鼠标垫", 60]]


def test_normalize_insert_values_leaves_arrays_and_garbage_alone():
    array_args = {"values": [[1, 2]]}
    _normalize_insert_values(array_args)
    assert array_args["values"] == [[1, 2]]

    garbage = {"values": "not-json"}
    _normalize_insert_values(garbage)
    assert garbage["values"] == "not-json"

    scalar = {"values": 3}
    _normalize_insert_values(scalar)
    assert scalar["values"] == 3

    missing: dict = {}
    _normalize_insert_values(missing)
    assert "values" not in missing


def test_synthetic_insert_diff_coordinates_start_at_column_a():
    diff = WorkspaceAgent._synthetic_insert_diff(
        {"start_row": 5, "values": [["鼠标垫", 29.9], ["键盘", 1]]}
    )
    assert diff == [
        {"cell": "A5", "before": None, "after": "鼠标垫"},
        {"cell": "B5", "before": None, "after": 29.9},
        {"cell": "A6", "before": None, "after": "键盘"},
        {"cell": "B6", "before": None, "after": 1},
    ]


def test_synthetic_insert_diff_unwraps_formula_wrapper():
    diff = WorkspaceAgent._synthetic_insert_diff(
        {"start_row": 3, "values": [["x", {"formula": "=B8*2"}]]}
    )
    assert diff == [
        {"cell": "A3", "before": None, "after": "x"},
        {"cell": "B3", "before": None, "after": "=B8*2"},
    ]


def test_synthetic_insert_diff_skips_non_grid_rows():
    diff = WorkspaceAgent._synthetic_insert_diff(
        {"start_row": 2, "values": [[1, 2], "not-a-row"]}
    )
    assert diff == [
        {"cell": "A2", "before": None, "after": 1},
        {"cell": "B2", "before": None, "after": 2},
    ]


def test_synthetic_insert_diff_rejects_invalid_args():
    assert WorkspaceAgent._synthetic_insert_diff({"start_row": 0, "values": [[1]]}) == []
    assert WorkspaceAgent._synthetic_insert_diff({"start_row": 2, "values": []}) == []
    assert WorkspaceAgent._synthetic_insert_diff({"start_row": "3", "values": [[1]]}) == []
    assert WorkspaceAgent._synthetic_insert_diff({"start_row": True, "values": [[1]]}) == []
    assert WorkspaceAgent._synthetic_insert_diff({"start_row": 3}) == []


def test_summary_insert_rows_mentions_written_rows():
    summary = summarize_operation(
        "insert_rows",
        "t.xlsx",
        {"sheet_name": "S", "start_row": 6, "count": 1, "values": [["a", "b"]]},
    )
    assert summary == "在 t.xlsx 工作表「S」第 6 行起插入 1 行并写入 1 行数据"


def test_summary_insert_rows_without_values_unchanged():
    summary = summarize_operation("insert_rows", "t.xlsx", {"sheet_name": "S", "start_row": 2})
    assert summary == "在 t.xlsx 工作表「S」第 2 行起插入 1 行"
