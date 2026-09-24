package com.raglatest.backend.service;

import java.util.regex.Matcher;
import java.util.regex.Pattern;
import tools.jackson.databind.JsonNode;
import tools.jackson.databind.node.ArrayNode;
import tools.jackson.databind.node.JsonNodeFactory;
import tools.jackson.databind.node.ObjectNode;

/**
 * 把待确认提案的坐标平移过一个已应用的行/列增删操作。
 *
 * <p>同一轮提交的提案共享同一份文件快照里的绝对行号。先应用其中一个（比如删两行）
 * 会搬移下方内容，之后再确认后续提案就会写错位置——删除了不该删的行、追加落在数据
 * 下方的空行上。每次成功应用后，由 {@link com.raglatest.backend.service.OperationService}
 * 调本类把同表其余待确认提案按净位移改写，使任意确认顺序都收敛到同一结果。
 * 目标行本身已被删掉的提案无法重定位，返回 null 交由调用方自动驳回。</p>
 *
 * <p>语义逐行移植自 Python services/operations.py 的 _rebase_arguments
 * （行/列算术、公式引用平移复用 {@link FormulaShift}，与 MCP 写入侧同一套规则）。</p>
 */
public final class OperationRebase {

    private OperationRebase() {}

    /** rebase 结果：改写后的参数 + 是否发生位移；null 表示提案目标已消失，须驳回。 */
    public record Rebased(ObjectNode arguments, boolean moved) {}

    private static final Pattern CELL = Pattern.compile("^([A-Za-z]{1,3})(\\d+)$");

    /**
     * @param toolName      待确认提案的工具名
     * @param args          提案参数（不会被就地修改）
     * @param delete        true=应用的是删除 [start, start+count)；false=插入
     * @param start         起始行/列号（1-based）
     * @param count         行/列数
     * @param columnAxis    true=列方向；false=行方向
     * @param affectedSheet 被增删行列的工作表名
     */
    public static Rebased rebaseArguments(String toolName, ObjectNode args,
                                          boolean delete, int start, int count,
                                          boolean columnAxis, String affectedSheet) {
        ShiftContext ctx = new ShiftContext(delete, start, count, columnAxis, sheetText(args.get("sheet_name")));

        switch (toolName) {
            case "delete_rows", "insert_rows", "delete_columns", "insert_columns" -> {
                // 另一轴向的待确认带没有本轴向的 start 值，int 解析失败即原样放行
                String bandKey = columnAxis ? "start_col" : "start_row";
                Integer bandStart = parseIntArg(args.get(bandKey));
                if (bandStart == null) {
                    return new Rebased(args, false);
                }
                Integer parsedCount = parseIntArg(args.get("count"));
                int bandCount = parsedCount == null || parsedCount == 0 ? 1 : parsedCount;
                if (delete) {
                    if (bandStart >= start + count) {
                        return withBand(args, bandKey, bandStart - count);
                    }
                    if ((toolName.equals("insert_rows") || toolName.equals("insert_columns"))
                            && bandStart >= start) {
                        // 插入点塌进被删带里：最近的合法位置就是带起点
                        return withBand(args, bandKey, start);
                    }
                    if (bandStart + bandCount <= start) {
                        return new Rebased(args, false);
                    }
                    return null; // 待确认带与已删带重叠，无法表达
                }
                if ((toolName.equals("delete_rows") || toolName.equals("delete_columns"))
                        && bandStart < start && start < bandStart + bandCount) {
                    return null; // 插入落在待删带内部，带不再连续
                }
                if (bandStart >= start) {
                    return withBand(args, bandKey, bandStart + count);
                }
                return new Rebased(args, false);
            }
            case "update_cells" -> {
                JsonNode updates = args.get("updates");
                if (updates != null && updates.isArray()) {
                    ArrayNode shifted = JsonNodeFactory.instance.arrayNode();
                    for (JsonNode item : updates) {
                        JsonNode entry = item.isObject() ? item.deepCopy() : item;
                        if (entry.isObject() && entry.has("cell")) {
                            ObjectNode object = (ObjectNode) entry;
                            object.set("cell", ctx.shiftCoordinate(object.get("cell")));
                            setOrPut(object, "value", ctx.shiftValue(object.get("value")));
                        }
                        shifted.add(entry);
                    }
                    if (ctx.dead) {
                        return null;
                    }
                    ObjectNode updated = (ObjectNode) args.deepCopy();
                    updated.set("updates", shifted);
                    return new Rebased(updated, ctx.moved);
                }
                return new Rebased(args, false);
            }
            case "set_formula" -> {
                JsonNode cell = ctx.shiftCoordinate(args.get("cell"));
                JsonNode formula = ctx.shiftValue(args.get("formula"));
                if (ctx.dead) {
                    return null;
                }
                ObjectNode updated = (ObjectNode) args.deepCopy();
                setOrPut(updated, "cell", cell);
                setOrPut(updated, "formula", formula);
                return new Rebased(updated, ctx.moved);
            }
            case "format_range" -> {
                JsonNode startCell = ctx.shiftCoordinate(args.get("start_cell"));
                JsonNode endCell = ctx.shiftCoordinate(args.get("end_cell"));
                if (ctx.dead) {
                    return null;
                }
                ObjectNode updated = (ObjectNode) args.deepCopy();
                setOrPut(updated, "start_cell", startCell);
                setOrPut(updated, "end_cell", endCell);
                return new Rebased(updated, ctx.moved);
            }
            case "delete_range" -> {
                JsonNode rangeText = ctx.shiftRangeText(args.get("range_text"));
                if (ctx.dead) {
                    return null;
                }
                ObjectNode updated = (ObjectNode) args.deepCopy();
                setOrPut(updated, "range_text", rangeText);
                return new Rebased(updated, ctx.moved);
            }
            case "copy_range" -> {
                // 只有真正落在被平移工作表上的那一侧跟随移动
                ObjectNode updated = (ObjectNode) args.deepCopy();
                String affected = affectedSheet == null ? "" : affectedSheet;
                if (affected.equals(sheetText(args.get("src_sheet")))) {
                    setOrPut(updated, "src_range", ctx.shiftRangeText(args.get("src_range")));
                }
                if (affected.equals(sheetText(args.get("dst_sheet")))) {
                    setOrPut(updated, "dst_cell", ctx.shiftCoordinate(args.get("dst_cell")));
                }
                if (ctx.dead) {
                    return null;
                }
                return new Rebased(updated, ctx.moved);
            }
            default -> {
                // merge/unmerge 钉在记录的坐标上（openpyxl 也不搬移合并区域）；
                // find_replace/manage_sheets 不带坐标，Word 工具从未有过。
                // 链式 digest 刷新兜底它们所有情况。
                return new Rebased(args, false);
            }
        }
    }

    private static Rebased withBand(ObjectNode args, String bandKey, int newValue) {
        ObjectNode updated = (ObjectNode) args.deepCopy();
        updated.put(bandKey, newValue);
        return new Rebased(updated, true);
    }

    // ------------------------------------------------------------------ //
    // 平移上下文与坐标算术
    // ------------------------------------------------------------------ //

    private static final class ShiftContext {
        boolean dead;
        boolean moved;
        final boolean delete;
        final int start;
        final int count;
        final boolean columnAxis;
        final String sheetName;

        ShiftContext(boolean delete, int start, int count, boolean columnAxis, String sheetName) {
            this.delete = delete;
            this.start = start;
            this.count = count;
            this.columnAxis = columnAxis;
            this.sheetName = sheetName;
        }

        /** 行（列）号过带后的新值；落在被删带内返回 null（目标已消失）。 */
        Integer lineAfter(int line) {
            if (delete) {
                if (start <= line && line < start + count) {
                    return null;
                }
                return line >= start + count ? line - count : line;
            }
            return line >= start ? line + count : line;
        }

        /** 平移一个 A1 单元格坐标（如 "B17"）；非坐标值原样返回。 */
        JsonNode shiftCoordinate(JsonNode value) {
            if (value == null || !value.isString()) {
                return value;
            }
            String text = value.asString().strip();
            Matcher match = CELL.matcher(text);
            if (!match.matches()) {
                return value;
            }
            String columnText = match.group(1);
            String rowText = match.group(2);
            String shifted;
            if (!columnAxis) {
                Integer line = lineAfter(Integer.parseInt(rowText));
                if (line == null) {
                    dead = true;
                    return value;
                }
                shifted = columnText + line;
            } else {
                Integer line = lineAfter(FormulaShift.columnIndex(columnText));
                if (line == null) {
                    dead = true;
                    return value;
                }
                shifted = FormulaShift.columnLetter(line) + rowText;
            }
            if (!shifted.equals(text)) {
                moved = true;
            }
            return JsonNodeFactory.instance.stringNode(shifted);
        }

        /** 平移 "A1:C4" 区间文本的两个端点（沿活跃轴向）。 */
        JsonNode shiftRangeText(JsonNode value) {
            if (value == null || !value.isString()) {
                return value;
            }
            String text = value.asString().strip();
            String[] parts = text.split(":", -1);
            if (parts.length != 2) {
                return value; // 畸形输入；后续 digest 校验会兜住
            }
            JsonNode left = shiftCoordinate(JsonNodeFactory.instance.stringNode(parts[0]));
            JsonNode right = shiftCoordinate(JsonNodeFactory.instance.stringNode(parts[1]));
            if (dead) {
                return value;
            }
            return JsonNodeFactory.instance.stringNode(left.asString() + ":" + right.asString());
        }

        /** 平移待写公式里的引用：{"formula": "=A1*2"} 包装与裸 "=A1*2" 两种形态。 */
        JsonNode shiftValue(JsonNode value) {
            if (value != null && value.isObject() && value.size() == 1
                    && value.hasNonNull("formula") && value.get("formula").isString()) {
                String original = value.get("formula").asString();
                String shifted = shiftFormulaText(original);
                if (!shifted.equals(original)) {
                    moved = true;
                }
                ObjectNode wrapper = JsonNodeFactory.instance.objectNode();
                wrapper.put("formula", shifted);
                return wrapper;
            }
            if (value != null && value.isString() && value.asString().startsWith("=")) {
                String original = value.asString();
                String shifted = shiftFormulaText(original);
                if (!shifted.equals(original)) {
                    moved = true;
                }
                return JsonNodeFactory.instance.stringNode(shifted);
            }
            return value;
        }

        private String shiftFormulaText(String formula) {
            // 待写公式按平移前的布局书写，其引用必须跟随目标单元格同向移动；
            // 复用 MCP 写入侧的同一实现，避免第二套平移规则
            String sheet = sheetName == null ? "" : sheetName;
            return FormulaShift.shiftFormulaReferences(
                    formula, sheet, sheet, !delete, start, count, columnAxis);
        }
    }

    // ------------------------------------------------------------------ //
    // JSON 工具
    // ------------------------------------------------------------------ //

    /** Python dict 写入 None → JSON null 的等价物；Jackson 3 的 set 不接受 null。 */
    private static void setOrPut(ObjectNode target, String key, JsonNode value) {
        if (value == null) {
            target.putNull(key);
        } else {
            target.set(key, value);
        }
    }

    /** sheet 名归一：缺失/null → null；文本 → 原文；其余 → JSON 文本（比较用）。 */
    static String sheetText(JsonNode value) {
        if (value == null || value.isNull()) {
            return null;
        }
        return value.isString() ? value.asString() : value.toString();
    }

    /** Python int(node)：数字截断、数字文本解析、其余失败返回 null。 */
    static Integer parseIntArg(JsonNode node) {
        if (node == null || node.isNull()) {
            return null;
        }
        if (node.isNumber()) {
            return node.asInt();
        }
        if (node.isString()) {
            try {
                return Integer.parseInt(node.asString().strip());
            } catch (NumberFormatException ignored) {
                return null;
            }
        }
        return null;
    }
}
