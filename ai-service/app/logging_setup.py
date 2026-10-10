"""索引链路的文件日志（分块明细全量落盘）。

标准做法沿用 Python Logging HOWTO 与各 FastAPI 生产模板的共识：应用日志走
stdlib logging，文件端用 ``RotatingFileHandler`` 按 maxBytes/backupCount 轮转；
uvicorn 自身的 access/error 日志不在本模块管辖内，互不干扰。

两级挂载，共用同一个 handler：

- ``app``        —— 包内所有 ``logging.getLogger(__name__)`` 的公共父级。INFO 级
                   摘要（批次开始/结束、失败告警）经 root 的 basicConfig 进控制台，
                   同时落日志文件；
- ``app.indexing`` —— 分块明细（逐 chunk 行、嵌入批次、向量写入）。propagate=False
                   且只挂文件 handler：一个文件可能产生上千行明细，控制台保持
                   干净，全量明细只落 ``data_dir/logs/ai-service.log``。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

APP_LOGGER = "app"
INDEX_LOGGER = f"{APP_LOGGER}.indexing"

# 单文件 10MB × 5 个备份，logs/ 总量封顶 ~60MB。
_MAX_BYTES = 10 * 1024 * 1024
_BACKUP_COUNT = 5

_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# 本模块安装的 handler，重复 configure / 关停时据此卸载（Windows 上残留句柄
# 会让临时目录与日志轮转出问题）。
_installed: list[RotatingFileHandler] = []


def index_logger() -> logging.Logger:
    """明细 logger：分块行、嵌入批次、向量写入等逐条记录（仅写文件）。"""
    return logging.getLogger(INDEX_LOGGER)


def text_preview(text: str | None, limit: int = 200) -> str:
    """单行化并截断的文本预览。

    换行/制表符转义成字面 ``\\n`` 保证一行日志一条记录；全文截断到 ``limit``
    字符并标注省略量——完整文本已经在 chunks 表里，日志里预览足够排障，
    否则一次大文件索引会把轮转文件瞬间打满。需要全文时调大 limit。
    """
    flattened = (text or "").replace("\\", "\\\\").replace("\n", "\\n")
    flattened = flattened.replace("\r", "\\r").replace("\t", "\\t")
    if len(flattened) <= limit:
        return flattened
    return f"{flattened[:limit]}…(+{len(flattened) - limit} chars)"


def configure_file_logging(settings: Any) -> Path:
    """把 ``app.*`` 日志落到 ``data_dir/logs/ai-service.log``。

    可重复调用：先卸载旧 handler 再按当前 settings 重装——测试会把 data_dir
    指到各自的临时目录，运行期只调用一次。返回日志文件路径。
    """
    close_file_logging()

    log_dir = settings.data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "ai-service.log"

    handler = RotatingFileHandler(
        log_path,
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(_FORMAT))

    app_logger = logging.getLogger(APP_LOGGER)
    # 显式 INFO：宿主进程（pytest/uvicorn）可能已给 root 挂过 handler，
    # main.py 的 basicConfig 会因此空转，root 级别不一定还是 INFO。
    app_logger.setLevel(logging.INFO)
    app_logger.addHandler(handler)

    detail = logging.getLogger(INDEX_LOGGER)
    detail.setLevel(logging.DEBUG)
    detail.propagate = False  # 明细不刷屏控制台，只走文件 handler
    detail.addHandler(handler)

    _installed.append(handler)
    app_logger.info("file logging enabled: %s", log_path)
    return log_path


def close_file_logging() -> None:
    """卸载并关闭本模块安装的文件 handler（应用关停与测试收尾时调用）。"""
    if not _installed:
        return
    app_logger = logging.getLogger(APP_LOGGER)
    detail = logging.getLogger(INDEX_LOGGER)
    for handler in _installed:
        app_logger.removeHandler(handler)
        detail.removeHandler(handler)
        handler.close()
    _installed.clear()
