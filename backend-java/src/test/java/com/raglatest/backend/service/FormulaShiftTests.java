package com.raglatest.backend.service;

import static org.assertj.core.api.Assertions.assertThat;

import org.junit.jupiter.api.Test;

/**
 * 公式引用平移的纯函数回归，用例逐条移植自 mcp-office-server/tests/test_formula_shift.py
 * （两侧必须保持同一套平移规则，这里钉住 Java 侧不再漂移）。
 */
class FormulaShiftTests {

    private static String shift(String formula, boolean insert, int start, int count,
                                String ownSheet, String affectedSheet, boolean columnAxis) {
        return FormulaShift.shiftFormulaReferences(
                formula, ownSheet, affectedSheet, insert, start, count, columnAxis);
    }

    private static String row(String formula, boolean insert, int start, int count) {
        return shift(formula, insert, start, count, "订单", "订单", false);
    }

    private static String row(String formula, int start, int count) {
        return row(formula, false, start, count);
    }

    private static String col(String formula, boolean insert, int start, int count) {
        return shift(formula, insert, start, count, "订单", "订单", true);
    }

    // ---------------- 行方向 ---------------- //

    @Test
    void deleteMovesLaterRowReferencesUp() {
        // 上报 bug 的回归：=C15*D15 必须跟随行平移
        assertThat(row("=C15*D15", 14, 1)).isEqualTo("=C14*D14");
        assertThat(row("=C17*D17", 14, 1)).isEqualTo("=C16*D16");
    }

    @Test
    void deleteLeavesReferencesAboveTheBandUntouched() {
        assertThat(row("=C10*D10", 14, 1)).isEqualTo("=C10*D10");
        assertThat(row("=C2*D2", 14, 3)).isEqualTo("=C2*D2");
    }

    @Test
    void deleteTurnsInBandReferencesIntoRefError() {
        assertThat(row("=C14*D14", 14, 1)).isEqualTo("=#REF!*#REF!");
    }

    @Test
    void insertMovesReferencesDown() {
        assertThat(row("=C14*D14", true, 14, 1)).isEqualTo("=C15*D15");
        assertThat(row("=C13*D13", true, 14, 2)).isEqualTo("=C13*D13");
    }

    @Test
    void rowAbsoluteReferencesDoNotMove() {
        assertThat(row("=C$14*D14", 14, 1)).isEqualTo("=C$14*#REF!");
        assertThat(row("=SUM(C$14:C20)", 14, 1)).isEqualTo("=SUM(C$14:C19)");
    }

    @Test
    void crossSheetReferencesOnlyShiftForTheAffectedSheet() {
        assertThat(shift("=Q1!D2", false, 2, 1, "汇总", "Q1", false)).isEqualTo("=Q1!#REF!");
        assertThat(shift("=Q1!D2", false, 2, 1, "汇总", "Q2", false)).isEqualTo("=Q1!D2");
        // 中文/带空格的 sheet 限定符
        assertThat(shift("=汇总!B2", false, 2, 1, "汇总", "汇总", false)).isEqualTo("=汇总!#REF!");
        assertThat(shift("='Q 1'!D2", false, 2, 1, "汇总", "Q 1", false)).isEqualTo("='Q 1'!#REF!");
    }

    @Test
    void rangesShrinkAndShift() {
        assertThat(row("=SUM(B2:D10)", 2, 1)).isEqualTo("=SUM(B2:D9)");
        assertThat(row("=SUM(B2:D10)", 14, 1)).isEqualTo("=SUM(B2:D10)");
        assertThat(row("=SUM(B2:D3)", 2, 2)).isEqualTo("=SUM(#REF!)");
        assertThat(row("=SUM(B2:D2)", true, 5, 1)).isEqualTo("=SUM(B2:D2)");
        assertThat(row("=SUM(B2:D2)", true, 2, 1)).isEqualTo("=SUM(B3:D3)");
    }

    @Test
    void functionNamesAndStringLiteralsAreNotTouched() {
        assertThat(row("=LOG10(A1)+1", 14, 1)).isEqualTo("=LOG10(A1)+1");
        assertThat(row("=IF(A1=\"R14\",\"是\",\"否\")", 14, 1)).isEqualTo("=IF(A1=\"R14\",\"是\",\"否\")");
        assertThat(row("=IF(B2>0,SUM(C2:C9),\"\")", 14, 1)).isEqualTo("=IF(B2>0,SUM(C2:C9),\"\")");
    }

    @Test
    void unqualifiedReferenceIsBoundToTheFormulaOwnSheet() {
        // 删的是其他 sheet 的行时，本 sheet 的无限定引用不能被误平移
        assertThat(shift("=Q1!D2+D9", false, 14, 1, "汇总", "订单", false)).isEqualTo("=Q1!D2+D9");
        assertThat(shift("=Q1!D2+D9", false, 9, 1, "汇总", "订单", false)).isEqualTo("=Q1!D2+D9");
        // 公式就在受影响 sheet 上时，无限定引用照常平移
        assertThat(shift("=Q1!D2+D9", false, 9, 1, "订单", "订单", false)).isEqualTo("=Q1!D2+#REF!");
    }

    @Test
    void rangeEndpointOrderIsPreserved() {
        assertThat(row("=SUM(B15:B17)", 14, 1)).isEqualTo("=SUM(B14:B16)");
    }

    // ---------------- 列方向 ---------------- //

    @Test
    void columnDeleteMovesLaterColumnReferencesLeft() {
        assertThat(col("=C2*E2", false, 2, 1)).isEqualTo("=B2*D2");
        assertThat(col("=B2*D2", false, 2, 1)).isEqualTo("=#REF!*C2");
    }

    @Test
    void columnDeleteTurnsInBandReferencesIntoRefError() {
        assertThat(col("=C15*D15", false, 3, 1)).isEqualTo("=#REF!*C15");
    }

    @Test
    void columnInsertMovesReferencesRight() {
        assertThat(col("=C14*D14", true, 3, 1)).isEqualTo("=D14*E14");
        assertThat(col("=C14*D14", true, 5, 1)).isEqualTo("=C14*D14");
    }

    @Test
    void columnAbsoluteReferencesDoNotMove() {
        // 既定语义（与行方向一致）：绝对端点钉死不动，即使指向被删的带
        assertThat(col("=$C15*$D15", false, 3, 1)).isEqualTo("=$C15*$D15");
        assertThat(col("=SUM($C2:E2)", false, 4, 2)).isEqualTo("=SUM($C2:C2)");
    }

    @Test
    void columnRangesShrinkAndShift() {
        assertThat(col("=SUM(B2:D2)", false, 3, 1)).isEqualTo("=SUM(B2:C2)");
        assertThat(col("=SUM(B2:D2)", false, 5, 1)).isEqualTo("=SUM(B2:D2)");
        assertThat(col("=SUM(C2:E2)", false, 3, 3)).isEqualTo("=SUM(#REF!)");
        assertThat(col("=SUM(C2:C2)", true, 3, 1)).isEqualTo("=SUM(D2:D2)");
        // 高低端点塌缩对称于行方向：删 C、D 后 B:D 只剩 B
        assertThat(col("=SUM(B2:D2)", false, 3, 2)).isEqualTo("=SUM(B2:B2)");
    }

    @Test
    void columnShiftOnlyForTheAffectedSheet() {
        assertThat(shift("=Q1!C2", false, 3, 1, "汇总", "Q1", true)).isEqualTo("=Q1!#REF!");
        assertThat(shift("=Q1!C2", false, 3, 1, "汇总", "Q2", true)).isEqualTo("=Q1!C2");
        assertThat(shift("=Q1!C2+C2", false, 3, 1, "汇总", "Q1", true)).isEqualTo("=Q1!#REF!+C2");
    }

    @Test
    void columnShiftPreservesReversedRangeOrder() {
        assertThat(col("=SUM(D2:B2)", false, 4, 1)).isEqualTo("=SUM(C2:B2)");
    }
}
