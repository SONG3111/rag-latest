# -*- coding: utf-8 -*-
"""Approval-order tests: applying one proposal must not desynchronise the others.

Proposals created in one turn share absolute row numbers from the same file
snapshot. Once the user applies a delete, every still-pending proposal on that
sheet must be re-pointed at the rows that still exist — whichever order the
cards are approved in. ``test_api_operations.py`` covers the plain apply flow;
these tests pin the rebase behaviour that keeps multi-proposal turns correct.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from openpyxl import Workbook, load_workbook

from app.services.operations import _rebase_arguments

pytestmark = pytest.mark.anyio

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def workbook_bytes() -> bytes:
    """Four data rows (2-5) so both an in-table edit and a trailing append exist."""
    book = Workbook()
    sheet = book.active
    sheet.title = "订单"
    sheet.append(["产品", "单价"])
    for name, price in [("甲", 10), ("乙", 20), ("丙", 30), ("丁", 40)]:
        sheet.append([name, price])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


def _seed_proposals(app, workspace_id: str) -> dict[str, str]:
    """The same-turn batch the agent would produce for '删第2行，追加新品并配公式'."""
    from app.services.operations import create_operation

    session = app.state.test_session_factory()
    try:
        delete_op = create_operation(
            session,
            workspace_id,
            "delete_rows",
            {"path": "行操作.xlsx", "sheet_name": "订单", "start_row": 2, "count": 1},
        )
        edit_op = create_operation(
            session,
            workspace_id,
            "update_cells",
            {
                "path": "行操作.xlsx",
                "sheet_name": "订单",
                "updates": [{"cell": "B5", "value": 41}],
            },
            diff=[{"cell": "B5", "before": 40, "after": 41}],
        )
        append_op = create_operation(
            session,
            workspace_id,
            "update_cells",
            {
                "path": "行操作.xlsx",
                "sheet_name": "订单",
                "updates": [{"cell": "B6", "value": 99}],
            },
            diff=[{"cell": "B6", "before": None, "after": 99}],
        )
        formula_op = create_operation(
            session,
            workspace_id,
            "set_formula",
            {
                "path": "行操作.xlsx",
                "sheet_name": "订单",
                "cell": "C6",
                "formula": "=B6*2",
            },
        )
        session.commit()
        return {
            "delete": delete_op.id,
            "edit": edit_op.id,
            "append": append_op.id,
            "formula": formula_op.id,
        }
    finally:
        session.close()


def _pending_args(app, workspace_id: str, operation_id: str) -> dict[str, Any]:
    session = app.state.test_session_factory()
    try:
        from app.models import Operation

        operation = session.get(Operation, operation_id)
        assert operation is not None
        return {
            "status": operation.status.value,
            "arguments": operation.arguments,
            "summary": operation.summary,
            "diff": operation.diff,
            "error": operation.error,
        }
    finally:
        session.close()


def _workbook_on_disk(app, workspace_id: str, filename: str = "行操作.xlsx"):
    from app.config import get_settings

    return get_settings().workspace_dir(workspace_id) / filename


async def _apply(client: AsyncClient, workspace_id: str, operation_id: str):
    return await client.post(
        f"/api/workspaces/{workspace_id}/operations/{operation_id}/apply"
    )


async def test_delete_first_rebases_the_followups(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            created = await client.post("/api/workspaces", json={"name": "先删后加"})
            workspace_id = created.json()["id"]
            await client.post(
                f"/api/workspaces/{workspace_id}/files",
                files=[("files", ("行操作.xlsx", workbook_bytes(), XLSX_MIME))],
            )
            ops = _seed_proposals(app_with_temp_storage, workspace_id)

            applied = await _apply(client, workspace_id, ops["delete"])
            assert applied.status_code == 200

            # 丁's price edit followed the row up (5 → 4) and its diff shows the
            # value that is actually in B4 now, not the pre-move snapshot.
            edit = _pending_args(app_with_temp_storage, workspace_id, ops["edit"])
            assert edit["arguments"]["updates"] == [{"cell": "B4", "value": 41}]
            assert edit["diff"] == [{"cell": "B4", "before": 40, "after": 41}]
            assert "B4" in edit["summary"]

            # The trailing append and its formula moved up into the vacated row.
            append = _pending_args(app_with_temp_storage, workspace_id, ops["append"])
            assert append["arguments"]["updates"] == [{"cell": "B5", "value": 99}]
            formula = _pending_args(app_with_temp_storage, workspace_id, ops["formula"])
            assert formula["arguments"]["cell"] == "C5"

            for key in ("edit", "append", "formula"):
                assert (await _apply(client, workspace_id, ops[key])).status_code == 200

            sheet = load_workbook(_workbook_on_disk(app_with_temp_storage, workspace_id))["订单"]
            assert [sheet.cell(row=r, column=1).value for r in (2, 3, 4)] == ["乙", "丙", "丁"]
            assert sheet["B4"].value == 41
            assert sheet["B5"].value == 99  # the append sits right after the data, no gap
            assert sheet["C5"].value == "=B5*2"
            assert sheet["B6"].value is None


async def test_append_first_converges_to_the_same_result(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            created = await client.post("/api/workspaces", json={"name": "先加后删"})
            workspace_id = created.json()["id"]
            await client.post(
                f"/api/workspaces/{workspace_id}/files",
                files=[("files", ("行操作.xlsx", workbook_bytes(), XLSX_MIME))],
            )
            ops = _seed_proposals(app_with_temp_storage, workspace_id)

            # Approve in the opposite order: append, formula, then the delete.
            for key in ("append", "formula"):
                assert (await _apply(client, workspace_id, ops[key])).status_code == 200

            # The delete proposal itself was never rebased (writes move no rows).
            delete_args = _pending_args(app_with_temp_storage, workspace_id, ops["delete"])
            assert delete_args["arguments"]["start_row"] == 2

            assert (await _apply(client, workspace_id, ops["delete"])).status_code == 200

            # openpyxl shifts the appended row up with the delete, so the same
            # final layout emerges as in the delete-first order.
            sheet = load_workbook(_workbook_on_disk(app_with_temp_storage, workspace_id))["订单"]
            assert [sheet.cell(row=r, column=1).value for r in (2, 3, 4)] == ["乙", "丙", "丁"]
            assert sheet["B5"].value == 99
            assert sheet["C5"].value == "=B5*2"
            assert sheet["B6"].value is None


async def test_proposal_whose_target_row_was_deleted_is_rejected(
    app_with_temp_storage,
) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            created = await client.post("/api/workspaces", json={"name": "目标已删"})
            workspace_id = created.json()["id"]
            await client.post(
                f"/api/workspaces/{workspace_id}/files",
                files=[("files", ("行操作.xlsx", workbook_bytes(), XLSX_MIME))],
            )
            from app.services.operations import create_operation

            session = app_with_temp_storage.state.test_session_factory()
            try:
                delete_op = create_operation(
                    session,
                    workspace_id,
                    "delete_rows",
                    {"path": "行操作.xlsx", "sheet_name": "订单", "start_row": 2, "count": 1},
                )
                stale_edit = create_operation(
                    session,
                    workspace_id,
                    "update_cells",
                    {
                        "path": "行操作.xlsx",
                        "sheet_name": "订单",
                        "updates": [{"cell": "B2", "value": 11}],
                    },
                )
                session.commit()
            finally:
                session.close()

            assert (await _apply(client, workspace_id, delete_op.id)).status_code == 200

            # Writing 11 into B2 would now hit 乙's row; the proposal is closed
            # instead of silently mutating a different product.
            stale = _pending_args(app_with_temp_storage, workspace_id, stale_edit.id)
            assert stale["status"] == "rejected"
            assert "自动驳回" in (stale["error"] or "")

            sheet = load_workbook(_workbook_on_disk(app_with_temp_storage, workspace_id))["订单"]
            assert sheet["B2"].value == 20


async def test_rebase_respects_other_sheets_on_the_same_file(
    app_with_temp_storage,
) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            created = await client.post("/api/workspaces", json={"name": "多表隔离"})
            workspace_id = created.json()["id"]

            book = Workbook()
            orders = book.active
            orders.title = "订单"
            orders.append(["产品", "单价"])
            orders.append(["甲", 10])
            orders.append(["甲", 10])
            archive = book.create_sheet("备份")
            archive["B9"] = 123
            buffer = io.BytesIO()
            book.save(buffer)

            await client.post(
                f"/api/workspaces/{workspace_id}/files",
                files=[("files", ("多表.xlsx", buffer.getvalue(), XLSX_MIME))],
            )
            from app.services.operations import create_operation

            session = app_with_temp_storage.state.test_session_factory()
            try:
                delete_op = create_operation(
                    session,
                    workspace_id,
                    "delete_rows",
                    {"path": "多表.xlsx", "sheet_name": "订单", "start_row": 2, "count": 1},
                )
                other_sheet = create_operation(
                    session,
                    workspace_id,
                    "update_cells",
                    {
                        "path": "多表.xlsx",
                        "sheet_name": "备份",
                        "updates": [{"cell": "B9", "value": 456}],
                    },
                )
                session.commit()
            finally:
                session.close()

            assert (await _apply(client, workspace_id, delete_op.id)).status_code == 200

            untouched = _pending_args(app_with_temp_storage, workspace_id, other_sheet.id)
            assert untouched["arguments"]["updates"] == [{"cell": "B9", "value": 456}]


# --------------------------------------------------------------------- #
# unit coverage for the coordinate arithmetic itself
# --------------------------------------------------------------------- #


def test_rebase_arguments_arithmetic() -> None:
    base = {"sheet_name": "订单"}

    # Applied delete(4,3): rows ≥7 shift up 3, rows 4-6 are gone.
    assert _rebase_arguments(
        "delete_rows", {**base, "start_row": 7, "count": 1}, kind="delete", start=4, count=3
    ) == ({**base, "start_row": 4, "count": 1}, True)
    assert _rebase_arguments(
        "delete_rows", {**base, "start_row": 2, "count": 2}, kind="delete", start=4, count=3
    ) == ({**base, "start_row": 2, "count": 2}, False)
    # An overlapping pending delete cannot be expressed any more.
    assert (
        _rebase_arguments(
            "delete_rows", {**base, "start_row": 5, "count": 4}, kind="delete", start=4, count=3
        )
        is None
    )
    # An insertion point inside the deleted band collapses to the band start.
    assert _rebase_arguments(
        "insert_rows", {**base, "start_row": 5}, kind="delete", start=4, count=3
    ) == ({**base, "start_row": 4}, True)
    # Applied insert(4,2): rows ≥4 shift down 2; a pending delete spanning the
    # insertion point no longer describes contiguous rows.
    assert _rebase_arguments(
        "delete_rows", {**base, "start_row": 2, "count": 2}, kind="insert", start=4, count=2
    ) == ({**base, "start_row": 2, "count": 2}, False)
    assert (
        _rebase_arguments(
            "delete_rows", {**base, "start_row": 3, "count": 4}, kind="insert", start=4, count=2
        )
        is None
    )
    assert _rebase_arguments(
        "delete_rows", {**base, "start_row": 5, "count": 1}, kind="insert", start=4, count=2
    ) == ({**base, "start_row": 7, "count": 1}, True)

    # Cell coordinates keep their column letters and follow their row.
    shifted, moved = _rebase_arguments(
        "update_cells",
        {**base, "updates": [{"cell": "B17", "value": 1}, {"cell": "E17", "value": 2}]},
        kind="delete",
        start=4,
        count=4,
    )
    assert moved and [u["cell"] for u in shifted["updates"]] == ["B13", "E13"]

    # One coordinate inside the deleted band invalidates the whole proposal.
    assert (
        _rebase_arguments(
            "update_cells",
            {**base, "updates": [{"cell": "B3", "value": 1}, {"cell": "B5", "value": 2}]},
            kind="delete",
            start=4,
            count=2,
        )
        is None
    )

    # A pending formula's references follow the same shift as its target cell.
    shifted, moved = _rebase_arguments(
        "set_formula",
        {**base, "cell": "C6", "formula": "=B6*2"},
        kind="delete",
        start=2,
        count=1,
    )
    assert moved and shifted["cell"] == "C5" and shifted["formula"] == "=B5*2"

    # A plain value is never touched.
    shifted, moved = _rebase_arguments(
        "update_cells",
        {**base, "updates": [{"cell": "B6", "value": "=B6*2"}]},
        kind="delete",
        start=2,
        count=1,
    )
    assert moved and shifted["updates"] == [{"cell": "B5", "value": "=B5*2"}]

    shifted, moved = _rebase_arguments(
        "format_range",
        {**base, "start_cell": "A6", "end_cell": "B7"},
        kind="delete",
        start=2,
        count=2,
    )
    assert moved and shifted["start_cell"] == "A4" and shifted["end_cell"] == "B5"

    # Word tools carry no spreadsheet rows; nothing moves.
    assert _rebase_arguments(
        "update_table_cell",
        {"table_index": 0, "row": 2, "column": 1, "value": "x"},
        kind="delete",
        start=2,
        count=2,
    ) == ({"table_index": 0, "row": 2, "column": 1, "value": "x"}, False)


def test_rebase_arguments_column_arithmetic() -> None:
    base = {"sheet_name": "订单"}

    # Applied delete_columns(3,1): columns ≥4 shift left 1, column 3 is gone.
    assert _rebase_arguments(
        "delete_columns", {**base, "start_col": 5, "count": 1},
        kind="delete", start=3, count=1, axis="col",
    ) == ({**base, "start_col": 4, "count": 1}, True)
    assert (
        _rebase_arguments(
            "delete_columns", {**base, "start_col": 3, "count": 1},
            kind="delete", start=3, count=1, axis="col",
        )
        is None
    )
    assert _rebase_arguments(
        "insert_columns", {**base, "start_col": 3},
        kind="delete", start=3, count=1, axis="col",
    ) == ({**base, "start_col": 3}, True)
    # Applied insert_columns(2,2): columns ≥2 shift right 2. A pending row-band
    # proposal has no start_col, so it stays untouched by a column shift.
    assert _rebase_arguments(
        "delete_columns", {**base, "start_col": 3, "count": 1},
        kind="insert", start=2, count=2, axis="col",
    ) == ({**base, "start_col": 5, "count": 1}, True)
    assert _rebase_arguments(
        "delete_rows", {**base, "start_row": 4, "count": 1},
        kind="insert", start=2, count=2, axis="col",
    ) == ({**base, "start_row": 4, "count": 1}, False)

    # Cell coordinates keep their row numbers and follow their column.
    shifted, moved = _rebase_arguments(
        "update_cells",
        {**base, "updates": [{"cell": "D5", "value": 1}, {"cell": "D9", "value": 2}]},
        kind="delete", start=3, count=1, axis="col",
    )
    assert moved and [u["cell"] for u in shifted["updates"]] == ["C5", "C9"]

    # A coordinate inside the deleted column band invalidates the proposal.
    assert (
        _rebase_arguments(
            "update_cells",
            {**base, "updates": [{"cell": "C5", "value": 1}]},
            kind="delete", start=3, count=1, axis="col",
        )
        is None
    )

    # A pending formula's references shift along the column axis too: the cell
    # moves with its column, and a reference into the deleted band dies.
    shifted, moved = _rebase_arguments(
        "set_formula",
        {**base, "cell": "C6", "formula": "=B6*2"},
        kind="delete", start=2, count=1, axis="col",
    )
    assert moved and shifted["cell"] == "B6" and shifted["formula"] == "=#REF!*2"
    shifted, moved = _rebase_arguments(
        "set_formula",
        {**base, "cell": "C6", "formula": "=D6*2"},
        kind="delete", start=2, count=1, axis="col",
    )
    assert moved and shifted["formula"] == "=C6*2"

    shifted, moved = _rebase_arguments(
        "format_range",
        {**base, "start_cell": "A6", "end_cell": "B7"},
        kind="insert", start=2, count=1, axis="col",
    )
    assert moved and shifted["start_cell"] == "A6" and shifted["end_cell"] == "C7"

    # delete_range moves both endpoints of its range text.
    shifted, moved = _rebase_arguments(
        "delete_range",
        {**base, "range_text": "C3:C8", "shift": "up"},
        kind="delete", start=2, count=1, axis="col",
    )
    assert moved and shifted["range_text"] == "B3:B8"
    assert (
        _rebase_arguments(
            "delete_range",
            {**base, "range_text": "C3:C8", "shift": "up"},
            kind="delete", start=3, count=2, axis="col",
        )
        is None
    )

    # copy_range follows only the side that sits on the affected sheet.
    shifted, moved = _rebase_arguments(
        "copy_range",
        {"src_sheet": "订单", "src_range": "D2:D9", "dst_sheet": "汇总", "dst_cell": "B2"},
        kind="delete", start=3, count=1, axis="col", affected_sheet="订单",
    )
    assert moved and shifted["src_range"] == "C2:C9" and shifted["dst_cell"] == "B2"
    # A source range inside the deleted column band cannot be relocated.
    assert (
        _rebase_arguments(
            "copy_range",
            {"src_sheet": "订单", "src_range": "C2:C9", "dst_sheet": "汇总", "dst_cell": "B2"},
            kind="delete", start=3, count=1, axis="col", affected_sheet="订单",
        )
        is None
    )
    shifted, moved = _rebase_arguments(
        "copy_range",
        {"src_sheet": "其他", "src_range": "C2:C9", "dst_sheet": "汇总", "dst_cell": "D2"},
        kind="delete", start=3, count=1, axis="col", affected_sheet="订单",
    )
    assert not moved and shifted["dst_cell"] == "D2"

    # merge/unmerge stay pinned (openpyxl does not move merged ranges either);
    # find_replace and manage_sheets carry no coordinates at all.
    for tool, args in (
        ("merge_cells", {**base, "range_text": "A1:C1"}),
        ("unmerge_cells", {**base, "range_text": "A1:C1"}),
        ("find_replace", {"query": "a", "replacement": "b"}),
        ("manage_sheets", {"action": "rename", "sheet_name": "a", "new_name": "b"}),
    ):
        assert _rebase_arguments(
            tool, args, kind="delete", start=3, count=1, axis="col", affected_sheet="订单"
        ) == (args, False)


async def test_column_delete_rebases_pending_edits(app_with_temp_storage) -> None:
    transport = ASGITransport(app=app_with_temp_storage)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        async with app_with_temp_storage.router.lifespan_context(app_with_temp_storage):
            created = await client.post("/api/workspaces", json={"name": "列删除平移"})
            workspace_id = created.json()["id"]

            book = Workbook()
            sheet = book.active
            sheet.title = "订单"
            sheet.append(["产品", "区域", "销售额"])
            sheet.append(["A型", "华东", 1000])
            buffer = io.BytesIO()
            book.save(buffer)

            await client.post(
                f"/api/workspaces/{workspace_id}/files",
                files=[("files", ("列操作.xlsx", buffer.getvalue(), XLSX_MIME))],
            )
            from app.services.operations import create_operation

            session = app_with_temp_storage.state.test_session_factory()
            try:
                drop_col = create_operation(
                    session,
                    workspace_id,
                    "delete_columns",
                    {"path": "列操作.xlsx", "sheet_name": "订单", "start_col": 2, "count": 1},
                )
                edit = create_operation(
                    session,
                    workspace_id,
                    "update_cells",
                    {
                        "path": "列操作.xlsx",
                        "sheet_name": "订单",
                        "updates": [{"cell": "C2", "value": 1500}],
                    },
                    diff=[{"cell": "C2", "before": 1000, "after": 1500}],
                )
                session.commit()
            finally:
                session.close()

            assert (await _apply(client, workspace_id, drop_col.id)).status_code == 200

            # The sales-figure edit followed its column left (C → B).
            rebased = _pending_args(app_with_temp_storage, workspace_id, edit.id)
            assert rebased["arguments"]["updates"] == [{"cell": "B2", "value": 1500}]
            assert "B2" in rebased["summary"]

            assert (await _apply(client, workspace_id, edit.id)).status_code == 200
            result = load_workbook(
                _workbook_on_disk(app_with_temp_storage, workspace_id, "列操作.xlsx")
            )["订单"]
            assert [result.cell(row=1, column=c).value for c in (1, 2)] == ["产品", "销售额"]
            assert result["B2"].value == 1500
