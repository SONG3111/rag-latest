# mcp-office-server

一个面向 Excel（`.xlsx` / `.xlsm`）与 Word（`.docx`）的 MCP Server，由
[FastMCP](https://github.com/modelcontextprotocol/python-sdk) 实现，通过 **stdio**
传输与本项目的后端通信。

## 设计要点

**工作区沙箱。** 所有工具的路径参数都是相对于工作区的相对路径。绝对路径、`../`
穿越、NUL 字节注入以及符号链接逃逸都会被拒绝。沙箱根由环境变量 `WORKSPACE_ROOT`
配置，支持用 `os.pathsep` 分隔多个根。

**无状态审批。** Server 不保存任何审批状态。每个工具都声明 MCP 标准注解：
`readOnlyHint=True` 的读取类工具可以直接调用，`destructiveHint=True` 的写入类
工具由宿主应用（后端）在用户确认后才真正执行。这样 Server 可以独立复用，审批策略
完全留在 Agent 层。

**精选工具面。** 只注册约 12 个核心工具。图表、透视表、文档保护、批注、脚注等能力
保留在实现层（`excel_ops` / `word_ops`）但不注册——工具过多会显著降低模型的工具
选择准确率。

**统一错误语义。** 工具不抛异常，而是返回
`{"ok": false, "error": {"code": ..., "message": ..., "detail": ...}}`，让 Agent
拿到可操作的错误信息而不是传输层失败。错误码见 `errors.py`。

## 工具清单

读取类（`readOnlyHint`）：

| 工具 | 说明 |
|---|---|
| `list_files` | 列出工作区内可操作的文档 |
| `get_doc_structure` | Excel 返回 sheet/行列/表头；Word 返回大纲与表格索引 |
| `read_range` | 读取 Excel 区域的值，公式与缓存值分列返回 |
| `read_paragraphs` | 按下标读取 Word 正文段落 |
| `read_table` | 读取 Word 中一张表的全部单元格 |
| `find_text` | 跨 sheet / 段落 / 表格定位文本 |

写入类（`destructiveHint`）：

| 工具 | 说明 |
|---|---|
| `update_cells` | 修改单元格值，返回逐格 before/after |
| `set_formula` | 写入公式 |
| `insert_rows` | 插入空行 |
| `delete_rows` | 删除行（不可逆） |
| `format_range` | 基础格式：粗斜体、字体色、填充色、数字格式、对齐 |
| `replace_text` | Word 查找替换，支持跨 run 匹配 |
| `update_table_cell` | 修改 Word 表格单元格文本 |

## 运行

```bash
# 作为 MCP 服务被宿主应用以 stdio 方式拉起
WORKSPACE_ROOT=/path/to/workspace python -m mcp_office_server.server
```

Windows PowerShell：

```powershell
$env:WORKSPACE_ROOT = "C:\path\to\workspace"
python -m mcp_office_server.server
```

未配置 `WORKSPACE_ROOT` 时 Server 会拒绝启动，而不是在没有沙箱的情况下操作文件系统。

## 测试

```bash
python -m pytest tests -q
```

## 许可

MIT。本项目是对两个 MIT 许可的上游项目的独立重写，来源与借鉴范围见
[NOTICE.md](NOTICE.md)。
