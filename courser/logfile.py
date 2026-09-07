"""持久化日志：写入 data/courser.log（gitignored），自动滚动。

所有运行痕迹（每轮登录/抓取/命中/发送/失败原因/风控）都会落盘，
便于排查"为什么登录不进去"这类问题。
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

from .config import DATA_DIR

LOG_FILE = Path(os.environ.get("COURSER_LOG", str(DATA_DIR / "courser.log")))
_MAX_BYTES = 2 * 1024 * 1024  # 单文件 2MB，超出滚动为 courser.log.1
_lock = threading.Lock()


def log(msg: str, level: str = "INFO") -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {msg}\n"
    try:
        with _lock:
            LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            if LOG_FILE.exists() and LOG_FILE.stat().st_size > _MAX_BYTES:
                LOG_FILE.replace(LOG_FILE.with_suffix(".log.1"))
            with LOG_FILE.open("a", encoding="utf-8") as f:
                f.write(line)
    except Exception:
        pass  # 日志失败绝不能影响主流程


def error(msg: str) -> None:
    log(msg, "ERROR")