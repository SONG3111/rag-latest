package com.raglatest.backend.service;

import java.util.regex.Matcher;
import java.util.regex.Pattern;

/**
 * 行/列插入删除后平移公式字符串里的 A1 引用。
 *
 * <p>openpyxl 在 delete_rows/insert_rows（及列变体）时会搬移单元格值，却刻意不动公式
 * 字符串（其已知局限，openpyxl issue #1273）：读 {@code =C15*D15} 的公式上移到第 14 行
 * 后仍引用第 15 行——那里现在是别的数据。Excel 本身会改写引用，本类为审批链路里的
 * 待写公式复刻该行为。规则遵循 Excel 语义：引用跟踪其目标单元格，而非公式自身位置。</p>
 *
 * <p>语义逐行移植自 mcp-office-server 的 formula_shift.py（MIT）；该模块同时服务
 * MCP 写入后的已写公式与这里的待写提案公式，两处必须保持同一套平移规则。</p>
 */
public final class FormulaShift {

    private FormulaShift() {}

    /**
     * 平移一条公式里的全部 A1 引用。
     *
     * @param formula       公式文本（如 {@code =C15*D15}）
     * @param ownSheet      公式所在工作表名（无限定引用归属它）
     * @param affectedSheet 被增删行列的工作表名（仅指向它的引用平移）
     * @param insert        true=在 start 处插入 count 行/列；false=删除 [start, start+count)
     * @param start         起始行/列号（1-based）
     * @param count         行/列数
     * @param columnAxis    true=列方向；false=行方向
     */
    public static String shiftFormulaReferences(String formula, String ownSheet, String affectedSheet,
                                                boolean insert, int start, int count, boolean columnAxis) {
        if (count <= 0) {
            return formula;
        }
        StringBuilder result = new StringBuilder();
        Matcher quoted = QUOTED.matcher(formula);
        int last = 0;
        // 引号内的字符串字面量原样保留，仅引号外做 token 平移（与 Python 的 split 语义一致）
        while (quoted.find()) {
            result.append(shiftSegment(formula.substring(last, quoted.start()),
                    ownSheet, affectedSheet, insert, start, count, columnAxis));
            result.append(quoted.group());
            last = quoted.end();
        }
        result.append(shiftSegment(formula.substring(last),
                ownSheet, affectedSheet, insert, start, count, columnAxis));
        return result.toString();
    }

    private static String shiftSegment(String segment, String ownSheet, String affectedSheet,
                                       boolean insert, int start, int count, boolean columnAxis) {
        StringBuilder out = new StringBuilder();
        Matcher matcher = TOKEN.matcher(segment);
        int last = 0;
        while (matcher.find()) {
            out.append(segment, last, matcher.start());
            String token = matcher.group();
            out.append(token.contains(":")
                    ? shiftRange(token, ownSheet, affectedSheet, insert, start, count, columnAxis)
                    : shiftCell(token, ownSheet, affectedSheet, insert, start, count, columnAxis));
            last = matcher.end();
        }
        out.append(segment.substring(last));
        return out.toString();
    }

    // ------------------------------------------------------------------ //
    // 端点解析与平移
    // ------------------------------------------------------------------ //

    /** 端点四元组：限定符（sheet 限定 + 列 $）、列字母、行号文本、两个绝对标记。 */
    private record Endpoint(String qualifier, String columnText, String rowText,
                            boolean columnAbsolute, boolean rowAbsolute) {}

    private static Endpoint splitEndpoint(String endpoint) {
        int bang = endpoint.lastIndexOf('!');
        String ref = bang >= 0 ? endpoint.substring(bang + 1) : endpoint;
        Matcher m = ENDPOINT.matcher(ref);
        if (!m.matches()) {
            // 由 TOKEN 正则保证可达性；防御性兜底：原样返回
            return new Endpoint(endpoint, "", "", false, false);
        }
        String qualifier = endpoint.substring(0, endpoint.length() - ref.length()) + ref.substring(0, m.start(2));
        return new Endpoint(qualifier, m.group(2), m.group(4),
                "$".equals(m.group(1)), "$".equals(m.group(3)));
    }

    /** 该引用 token 是否指向被平移的工作表（无限定引用归属公式所在表）。 */
    private static boolean pointsAtAffectedSheet(String token, String ownSheet, String affectedSheet) {
        int bang = token.indexOf('!');
        if (bang < 0) {
            return ownSheet != null && affectedSheet != null
                    && ownSheet.equalsIgnoreCase(affectedSheet);
        }
        String qualifier = token.substring(0, bang);
        String target = qualifier.startsWith("'")
                ? qualifier.substring(1, qualifier.length() - 1).replace("''", "'")
                : qualifier;
        return target != null && affectedSheet != null && target.equalsIgnoreCase(affectedSheet);
    }

    private static String shiftCell(String token, String ownSheet, String affectedSheet,
                                    boolean insert, int start, int count, boolean columnAxis) {
        if (!pointsAtAffectedSheet(token, ownSheet, affectedSheet)) {
            return token;
        }
        Endpoint endpoint = splitEndpoint(token);
        if (!columnAxis) {
            if (endpoint.rowAbsolute()) {
                return token;
            }
            int row = Integer.parseInt(endpoint.rowText());
            if (insert) {
                if (row >= start) {
                    return endpoint.qualifier() + endpoint.columnText() + (row + count);
                }
            } else {
                if (row >= start + count) {
                    return endpoint.qualifier() + endpoint.columnText() + (row - count);
                }
                if (row >= start) {
                    return endpoint.qualifier() + "#REF!";
                }
            }
            return token;
        }
        if (endpoint.columnAbsolute()) {
            return token;
        }
        int column = columnIndex(endpoint.columnText());
        if (insert) {
            if (column >= start) {
                return endpoint.qualifier() + columnLetter(column + count) + endpoint.rowText();
            }
        } else {
            if (column >= start + count) {
                return endpoint.qualifier() + columnLetter(column - count) + endpoint.rowText();
            }
            if (column >= start) {
                return endpoint.qualifier() + "#REF!";
            }
        }
        return token;
    }

    private static String shiftRange(String token, String ownSheet, String affectedSheet,
                                     boolean insert, int start, int count, boolean columnAxis) {
        int colon = token.indexOf(':');
        String left = token.substring(0, colon);
        String right = token.substring(colon + 1);
        String probe = left.contains("!") ? left : (right.contains("!") ? right : left);
        if (!pointsAtAffectedSheet(probe, ownSheet, affectedSheet)) {
            return token;
        }

        Endpoint first = splitEndpoint(left);
        Endpoint second = splitEndpoint(right);
        int band1 = columnAxis ? columnIndex(first.columnText()) : Integer.parseInt(first.rowText());
        int band2 = columnAxis ? columnIndex(second.columnText()) : Integer.parseInt(second.rowText());
        boolean abs1 = columnAxis ? first.columnAbsolute() : first.rowAbsolute();
        boolean abs2 = columnAxis ? second.columnAbsolute() : second.rowAbsolute();

        // 完全落在被删带内的区间整段失去所覆盖的行
        if (!insert && start <= Math.min(band1, band2) && Math.max(band1, band2) < start + count) {
            return "#REF!";
        }

        boolean high1 = band1 >= band2;
        int new1 = shiftedBand(band1, abs1, high1, insert, start, count);
        int new2 = shiftedBand(band2, abs2, !high1, insert, start, count);

        String part1;
        String part2;
        if (columnAxis) {
            part1 = abs1 ? left : first.qualifier() + columnLetter(new1) + first.rowText();
            part2 = abs2 ? right : second.qualifier() + columnLetter(new2) + second.rowText();
        } else {
            part1 = abs1 ? left : first.qualifier() + first.columnText() + new1;
            part2 = abs2 ? right : second.qualifier() + second.columnText() + new2;
        }
        return part1 + ":" + part2;
    }

    /** 带内端点向带边塌缩（高端点到 start-1、低端点到 start），带外按方向平移。 */
    private static int shiftedBand(int band, boolean absolute, boolean high,
                                   boolean insert, int start, int count) {
        if (absolute) {
            return band;
        }
        if (insert) {
            return band >= start ? band + count : band;
        }
        int bandEnd = start + count;
        if (band >= bandEnd) {
            return band - count;
        }
        if (band >= start) {
            return high ? start - 1 : start;
        }
        return band;
    }

    // ------------------------------------------------------------------ //
    // 列号 ↔ 列字母（openpyxl column_index_from_string / get_column_letter 语义）
    // ------------------------------------------------------------------ //

    static int columnIndex(String columnText) {
        int index = 0;
        for (char c : columnText.toUpperCase().toCharArray()) {
            index = index * 26 + (c - 'A' + 1);
        }
        return index;
    }

    static String columnLetter(int index) {
        StringBuilder letters = new StringBuilder();
        while (index > 0) {
            int rem = (index - 1) % 26;
            letters.insert(0, (char) ('A' + rem));
            index = (index - 1) / 26;
        }
        return letters.toString();
    }

    // ------------------------------------------------------------------ //
    // 正则（与 formula_shift.py 逐条对应）
    // ------------------------------------------------------------------ //

    /** Excel 禁止出现在裸 sheet 名里的字符，加上会导致限定符歧义的符号。 */
    private static final String BARE_SHEET = "[^'!:;()+*/\\[\\]\\\\#&=,?\\s]";

    private static final String CELL_TEXT = "\\$?[A-Z]{1,3}\\$?\\d{1,7}";

    private static final String QUALIFIER_TEXT = "(?:'[^']+'|" + BARE_SHEET + "+)!";

    /**
     * 一个引用 token：可选 sheet 限定符 + 单元格 + 冒号后的第二个单元格。
     * 守卫：前面不是更长标识符的尾部（LOG10）；行号后不跟数字、单元格后不跟 "("。
     */
    private static final Pattern TOKEN = Pattern.compile(
            "(?<![A-Za-z0-9_.])(?:" + QUALIFIER_TEXT + ")?" + CELL_TEXT + "(?![\\d(])"
                    + "(?::(?:" + QUALIFIER_TEXT + ")?" + CELL_TEXT + "(?![\\d(])?)?");

    private static final Pattern ENDPOINT = Pattern.compile("^(\\$?)([A-Z]{1,3})(\\$?)(\\d{1,7})$");

    /** 双引号字符串字面量（内部 "" 转义），平移时原样跳过。 */
    private static final Pattern QUOTED = Pattern.compile("(\"(?:[^\"]|\"\")*\")");
}
