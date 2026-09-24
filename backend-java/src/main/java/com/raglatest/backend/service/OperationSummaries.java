package com.raglatest.backend.service;

import java.util.Map;
import tools.jackson.databind.JsonNode;

/**
 * 审批卡片上的一行中文摘要。逐字移植自 Python services/operations.py 的
 * summarize_operation——提案落库与 rebase 改写参数后都要用同一套措辞，
 * 用户在两个时点看到的卡片文案才一致。
 */
public final class OperationSummaries {

    private OperationSummaries() {}

    private static final Map<String, String> SHEET_ACTIONS =
            Map.of("create", "新建", "rename", "重命名", "copy", "复制", "delete", "删除");

    public static String summarize(String toolName, String relPath, JsonNode arguments) {
        JsonNode args = arguments;
        switch (toolName) {
            case "update_cells" -> {
                String sheet = text(args, "sheet_name");
                StringBuilder coords = new StringBuilder();
                JsonNode cells = args == null ? null : args.get("updates");
                if (cells != null && cells.isArray()) {
                    for (JsonNode item : cells) {
                        if (!item.isObject()) {
                            continue;
                        }
                        if (!coords.isEmpty()) {
                            coords.append(", ");
                        }
                        coords.append(cellText(item));
                    }
                }
                return "修改 " + relPath + " 工作表「" + sheet + "」中的单元格：" + coords;
            }
            case "set_formula" -> {
                return "向 " + relPath + " 工作表「" + text(args, "sheet_name") + "」的 "
                        + text(args, "cell") + " 写入公式";
            }
            case "insert_rows" -> {
                return "在 " + relPath + " 工作表「" + text(args, "sheet_name") + "」第 "
                        + text(args, "start_row") + " 行起插入 " + countOr(args, 1) + " 行";
            }
            case "delete_rows" -> {
                return "删除 " + relPath + " 工作表「" + text(args, "sheet_name") + "」第 "
                        + text(args, "start_row") + " 行起的 " + countOr(args, 1) + " 行（不可逆）";
            }
            case "format_range" -> {
                return "为 " + relPath + " 的 " + text(args, "start_cell") + ":" + text(args, "end_cell")
                        + " 设置格式";
            }
            case "insert_columns" -> {
                return "在 " + relPath + " 工作表「" + text(args, "sheet_name") + "」第 "
                        + text(args, "start_col") + " 列起插入 " + countOr(args, 1) + " 列";
            }
            case "delete_columns" -> {
                return "删除 " + relPath + " 工作表「" + text(args, "sheet_name") + "」第 "
                        + text(args, "start_col") + " 列起的 " + countOr(args, 1) + " 列（不可逆）";
            }
            case "copy_range" -> {
                return "把 " + relPath + " 的 " + text(args, "src_sheet") + "!" + text(args, "src_range")
                        + " 复制到 " + text(args, "dst_sheet") + "!" + text(args, "dst_cell");
            }
            case "delete_range" -> {
                return "删除 " + relPath + " 工作表「" + text(args, "sheet_name") + "」的区域 "
                        + text(args, "range_text") + "（" + textOr(args, "shift", "up") + " 补位，不可逆）";
            }
            case "merge_cells" -> {
                return "合并 " + relPath + " 工作表「" + text(args, "sheet_name") + "」的 "
                        + text(args, "range_text");
            }
            case "unmerge_cells" -> {
                return "取消 " + relPath + " 工作表「" + text(args, "sheet_name") + "」"
                        + text(args, "range_text") + " 的合并";
            }
            case "find_replace" -> {
                String scope = args != null && args.hasNonNull("sheet_name")
                        ? "工作表「" + text(args, "sheet_name") + "」" : "全部工作表";
                return "在 " + relPath + " " + scope + "中把「" + text(args, "query")
                        + "」替换为「" + text(args, "replacement") + "」";
            }
            case "manage_sheets" -> {
                String action = text(args, "action");
                String actionCn = SHEET_ACTIONS.getOrDefault(action, action);
                String target = firstNonEmptyText(args, "new_name", "sheet_name");
                return actionCn + " " + relPath + " 的工作表「" + target + "」";
            }
            case "replace_text" -> {
                return "在 " + relPath + " 中把「" + text(args, "find") + "」替换为「"
                        + text(args, "replace") + "」";
            }
            case "update_table_cell" -> {
                return "修改 " + relPath + " 表格 " + text(args, "table_index") + " 第 "
                        + text(args, "row") + " 行第 " + text(args, "column") + " 列";
            }
            default -> {
                return "对 " + relPath + " 执行 " + toolName;
            }
        }
    }

    /** Python str(item.get("cell"))：字符串去引号，数字原样，缺失为 "None"。 */
    private static String cellText(JsonNode item) {
        JsonNode cell = item.get("cell");
        if (cell == null || cell.isNull()) {
            return "None";
        }
        return cell.isString() ? cell.asString() : cell.toString();
    }

    private static String text(JsonNode args, String key) {
        JsonNode value = args == null ? null : args.get(key);
        if (value == null || value.isNull()) {
            return "";
        }
        return value.isString() ? value.asString() : value.toString();
    }

    private static String textOr(JsonNode args, String key, String fallback) {
        JsonNode value = args == null ? null : args.get(key);
        if (value == null || value.isNull()) {
            return fallback;
        }
        return value.isString() ? value.asString() : value.toString();
    }

    private static String firstNonEmptyText(JsonNode args, String... keys) {
        for (String key : keys) {
            JsonNode value = args == null ? null : args.get(key);
            if (value == null || value.isNull()) {
                continue;
            }
            String text = value.isString() ? value.asString() : value.toString();
            if (!text.isEmpty()) {
                return text;
            }
        }
        return "";
    }

    /** arguments.get("count", 1)：缺失或 0 时按 1 展示。 */
    private static String countOr(JsonNode args, int fallback) {
        JsonNode value = args == null ? null : args.get("count");
        if (value == null || value.isNull()) {
            return String.valueOf(fallback);
        }
        if (value.isNumber()) {
            int parsed = value.asInt(fallback);
            return String.valueOf(parsed == 0 ? fallback : parsed);
        }
        if (value.isString()) {
            try {
                int parsed = Integer.parseInt(value.asString().strip());
                return String.valueOf(parsed == 0 ? fallback : parsed);
            } catch (NumberFormatException ignored) {
                return String.valueOf(fallback);
            }
        }
        return String.valueOf(fallback);
    }
}
