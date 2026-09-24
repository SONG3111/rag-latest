package com.raglatest.backend.service;

import static org.assertj.core.api.Assertions.assertThat;

import com.raglatest.backend.service.OperationRebase.Rebased;
import org.junit.jupiter.api.Test;
import tools.jackson.databind.ObjectMapper;
import tools.jackson.databind.node.ObjectNode;

/**
 * 坐标平移算术本身的单测，用例逐条移植自 ai-service/tests/test_proposal_rebase.py 的
 * test_rebase_arguments_arithmetic / test_rebase_arguments_column_arithmetic。
 */
class OperationRebaseTests {

    private static final ObjectMapper MAPPER = new ObjectMapper();

    private static ObjectNode args(String json) {
        return (ObjectNode) MAPPER.readTree(json);
    }

    private static Rebased rebase(String tool, String json, boolean delete,
                                  int start, int count, boolean colAxis, String affectedSheet) {
        return OperationRebase.rebaseArguments(tool, args(json), delete, start, count, colAxis, affectedSheet);
    }

    private static RebaseTestsHelper row(String tool, String json, boolean delete, int start, int count) {
        return new RebaseTestsHelper(tool, json, delete, start, count, false, null);
    }

    private static RebaseTestsHelper col(String tool, String json, boolean delete, int start, int count,
                                         String affectedSheet) {
        return new RebaseTestsHelper(tool, json, delete, start, count, true, affectedSheet);
    }

    /** 流式断言辅助：moved()/dead()/field()。 */
    private record RebaseTestsHelper(String tool, String json, boolean delete, int start,
                                     int count, boolean colAxis, String affectedSheet) {
        Rebased call() {
            return OperationRebase.rebaseArguments(tool, args(json), delete, start, count, colAxis, affectedSheet);
        }
    }

    private static String cellAt(Rebased result, int index) {
        return result.arguments().path("updates").get(index).path("cell").asString();
    }

    // ---------------- 行方向算术 ---------------- //

    @Test
    void rowBandArithmeticFollowsAppliedDelete() {
        // 应用 delete(4,3)：≥7 的行上移 3，行 4-6 已消失
        Rebased shifted = row("delete_rows",
                "{\"sheet_name\":\"订单\",\"start_row\":7,\"count\":1}", true, 4, 3).call();
        assertThat(shifted.moved()).isTrue();
        assertThat(shifted.arguments().path("start_row").asInt()).isEqualTo(4);

        Rebased untouched = row("delete_rows",
                "{\"sheet_name\":\"订单\",\"start_row\":2,\"count\":2}", true, 4, 3).call();
        assertThat(untouched.moved()).isFalse();
        assertThat(untouched.arguments().path("start_row").asInt()).isEqualTo(2);

        // 与被删带重叠的待确认 delete 无法再表达
        assertThat(row("delete_rows",
                "{\"sheet_name\":\"订单\",\"start_row\":5,\"count\":4}", true, 4, 3).call()).isNull();

        // 插入点落在被删带内：最近的合法位置是带起点
        Rebased collapsed = row("insert_rows",
                "{\"sheet_name\":\"订单\",\"start_row\":5}", true, 4, 3).call();
        assertThat(collapsed.moved()).isTrue();
        assertThat(collapsed.arguments().path("start_row").asInt()).isEqualTo(4);
    }

    @Test
    void rowBandArithmeticFollowsAppliedInsert() {
        // 应用 insert(4,2)：≥4 的行下移 2；横跨插入点的待删带不再连续
        assertThat(row("delete_rows",
                "{\"sheet_name\":\"订单\",\"start_row\":2,\"count\":2}", false, 4, 2).call().moved()).isFalse();
        assertThat(row("delete_rows",
                "{\"sheet_name\":\"订单\",\"start_row\":3,\"count\":4}", false, 4, 2).call()).isNull();

        Rebased moved = row("delete_rows",
                "{\"sheet_name\":\"订单\",\"start_row\":5,\"count\":1}", false, 4, 2).call();
        assertThat(moved.moved()).isTrue();
        assertThat(moved.arguments().path("start_row").asInt()).isEqualTo(7);
    }

    @Test
    void cellCoordinatesKeepColumnLettersAndFollowTheirRow() {
        Rebased shifted = row("update_cells",
                "{\"sheet_name\":\"订单\",\"updates\":["
                        + "{\"cell\":\"B17\",\"value\":1},{\"cell\":\"E17\",\"value\":2}]}",
                true, 4, 4).call();
        assertThat(shifted.moved()).isTrue();
        assertThat(cellAt(shifted, 0)).isEqualTo("B13");
        assertThat(cellAt(shifted, 1)).isEqualTo("E13");
    }

    @Test
    void coordinateInsideDeletedBandInvalidatesWholeProposal() {
        assertThat(row("update_cells",
                "{\"sheet_name\":\"订单\",\"updates\":["
                        + "{\"cell\":\"B3\",\"value\":1},{\"cell\":\"B5\",\"value\":2}]}",
                true, 4, 2).call()).isNull();
    }

    @Test
    void pendingFormulaReferencesFollowTheSameShiftAsTheirTargetCell() {
        Rebased shifted = row("set_formula",
                "{\"sheet_name\":\"订单\",\"cell\":\"C6\",\"formula\":\"=B6*2\"}", true, 2, 1).call();
        assertThat(shifted.moved()).isTrue();
        assertThat(shifted.arguments().path("cell").asString()).isEqualTo("C5");
        assertThat(shifted.arguments().path("formula").asString()).isEqualTo("=B5*2");
    }

    @Test
    void formulaWrappedInObjectIsShiftedToo() {
        Rebased shifted = row("update_cells",
                "{\"sheet_name\":\"订单\",\"updates\":["
                        + "{\"cell\":\"B6\",\"value\":{\"formula\":\"=B6*2\"}}]}",
                true, 2, 1).call();
        assertThat(shifted.moved()).isTrue();
        assertThat(shifted.arguments().path("updates").get(0).path("value").path("formula").asString())
                .isEqualTo("=B5*2");
    }

    @Test
    void formatRangeEndpointsShift() {
        Rebased shifted = row("format_range",
                "{\"sheet_name\":\"订单\",\"start_cell\":\"A6\",\"end_cell\":\"B7\"}", true, 2, 2).call();
        assertThat(shifted.moved()).isTrue();
        assertThat(shifted.arguments().path("start_cell").asString()).isEqualTo("A4");
        assertThat(shifted.arguments().path("end_cell").asString()).isEqualTo("B5");
    }

    @Test
    void wordToolsCarryNoSpreadsheetRows() {
        Rebased shifted = row("update_table_cell",
                "{\"table_index\":0,\"row\":2,\"column\":1,\"value\":\"x\"}", true, 2, 2).call();
        assertThat(shifted.moved()).isFalse();
        assertThat(shifted.arguments().path("row").asInt()).isEqualTo(2);
    }

    // ---------------- 列方向算术 ---------------- //

    @Test
    void columnBandArithmetic() {
        // 应用 delete_columns(3,1)：≥4 的列左移 1，列 3 消失
        Rebased shifted = col("delete_columns",
                "{\"sheet_name\":\"订单\",\"start_col\":5,\"count\":1}", true, 3, 1, null).call();
        assertThat(shifted.moved()).isTrue();
        assertThat(shifted.arguments().path("start_col").asInt()).isEqualTo(4);

        assertThat(col("delete_columns",
                "{\"sheet_name\":\"订单\",\"start_col\":3,\"count\":1}", true, 3, 1, null).call()).isNull();

        Rebased collapsed = col("insert_columns",
                "{\"sheet_name\":\"订单\",\"start_col\":3}", true, 3, 1, null).call();
        assertThat(collapsed.moved()).isTrue();
        assertThat(collapsed.arguments().path("start_col").asInt()).isEqualTo(3);

        // 应用 insert_columns(2,2)：≥2 的列右移 2；行带提案没有 start_col，列移不影响
        Rebased pushed = col("delete_columns",
                "{\"sheet_name\":\"订单\",\"start_col\":3,\"count\":1}", false, 2, 2, null).call();
        assertThat(pushed.moved()).isTrue();
        assertThat(pushed.arguments().path("start_col").asInt()).isEqualTo(5);

        Rebased rowBand = col("delete_rows",
                "{\"sheet_name\":\"订单\",\"start_row\":4,\"count\":1}", false, 2, 2, null).call();
        assertThat(rowBand.moved()).isFalse();
        assertThat(rowBand.arguments().path("start_row").asInt()).isEqualTo(4);
    }

    @Test
    void cellCoordinatesFollowTheirColumn() {
        Rebased shifted = col("update_cells",
                "{\"sheet_name\":\"订单\",\"updates\":["
                        + "{\"cell\":\"D5\",\"value\":1},{\"cell\":\"D9\",\"value\":2}]}",
                true, 3, 1, null).call();
        assertThat(shifted.moved()).isTrue();
        assertThat(cellAt(shifted, 0)).isEqualTo("C5");
        assertThat(cellAt(shifted, 1)).isEqualTo("C9");
    }

    @Test
    void coordinateInsideDeletedColumnBandInvalidatesProposal() {
        assertThat(col("update_cells",
                "{\"sheet_name\":\"订单\",\"updates\":[{\"cell\":\"C5\",\"value\":1}]}",
                true, 3, 1, null).call()).isNull();
    }

    @Test
    void formulaReferencesShiftAlongTheColumnAxis() {
        Rebased dead = col("set_formula",
                "{\"sheet_name\":\"订单\",\"cell\":\"C6\",\"formula\":\"=B6*2\"}", true, 2, 1, null).call();
        assertThat(dead.moved()).isTrue();
        assertThat(dead.arguments().path("cell").asString()).isEqualTo("B6");
        assertThat(dead.arguments().path("formula").asString()).isEqualTo("=#REF!*2");

        Rebased shifted = col("set_formula",
                "{\"sheet_name\":\"订单\",\"cell\":\"C6\",\"formula\":\"=D6*2\"}", true, 2, 1, null).call();
        assertThat(shifted.arguments().path("formula").asString()).isEqualTo("=C6*2");
    }

    @Test
    void formatRangeColumnsShift() {
        Rebased shifted = col("format_range",
                "{\"sheet_name\":\"订单\",\"start_cell\":\"A6\",\"end_cell\":\"B7\"}", false, 2, 1, null).call();
        assertThat(shifted.moved()).isTrue();
        assertThat(shifted.arguments().path("start_cell").asString()).isEqualTo("A6");
        assertThat(shifted.arguments().path("end_cell").asString()).isEqualTo("C7");
    }

    @Test
    void deleteRangeMovesBothEndpoints() {
        Rebased shifted = col("delete_range",
                "{\"sheet_name\":\"订单\",\"range_text\":\"C3:C8\",\"shift\":\"up\"}", true, 2, 1, null).call();
        assertThat(shifted.moved()).isTrue();
        assertThat(shifted.arguments().path("range_text").asString()).isEqualTo("B3:B8");

        assertThat(col("delete_range",
                "{\"sheet_name\":\"订单\",\"range_text\":\"C3:C8\",\"shift\":\"up\"}", true, 3, 2, null)
                .call()).isNull();
    }

    @Test
    void copyRangeFollowsOnlyTheSideOnTheAffectedSheet() {
        RebaseTestsHelper helper = col("copy_range",
                "{\"src_sheet\":\"订单\",\"src_range\":\"D2:D9\",\"dst_sheet\":\"汇总\",\"dst_cell\":\"B2\"}",
                true, 3, 1, "订单");
        Rebased shifted = helper.call();
        assertThat(shifted.moved()).isTrue();
        assertThat(shifted.arguments().path("src_range").asString()).isEqualTo("C2:C9");
        assertThat(shifted.arguments().path("dst_cell").asString()).isEqualTo("B2");

        // 源区间落在被删列带内：无法重定位
        assertThat(col("copy_range",
                "{\"src_sheet\":\"订单\",\"src_range\":\"C2:C9\",\"dst_sheet\":\"汇总\",\"dst_cell\":\"B2\"}",
                true, 3, 1, "订单").call()).isNull();

        // 源/目的地都不在被平移的表上：不动
        Rebased untouched = col("copy_range",
                "{\"src_sheet\":\"其他\",\"src_range\":\"C2:C9\",\"dst_sheet\":\"汇总\",\"dst_cell\":\"D2\"}",
                true, 3, 1, "订单").call();
        assertThat(untouched.moved()).isFalse();
        assertThat(untouched.arguments().path("dst_cell").asString()).isEqualTo("D2");
    }

    @Test
    void mergeAndSheetlessToolsStayPinned() {
        // merge/unmerge 钉在记录的坐标上（openpyxl 也不搬移合并区域）；
        // find_replace/manage_sheets 不带坐标
        for (String tool : java.util.List.of(
                "merge_cells", "unmerge_cells")) {
            Rebased shifted = col(tool,
                    "{\"sheet_name\":\"订单\",\"range_text\":\"A1:C1\"}", true, 3, 1, "订单").call();
            assertThat(shifted.moved()).isFalse();
            assertThat(shifted.arguments().path("range_text").asString()).isEqualTo("A1:C1");
        }
        Rebased findReplace = col("find_replace",
                "{\"query\":\"a\",\"replacement\":\"b\"}", true, 3, 1, "订单").call();
        assertThat(findReplace.moved()).isFalse();
        RebaseTestsHelper manage = col("manage_sheets",
                "{\"action\":\"rename\",\"sheet_name\":\"a\",\"new_name\":\"b\"}", true, 3, 1, "订单");
        assertThat(manage.call().moved()).isFalse();
    }
}
