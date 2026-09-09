"""刷课机警告触发率（风控风险评估）。

风控 = 最近 N 次**有效抓取**中，实际检测到「请勿使用刷课机 / 操作频繁」等警告的比例。

- **有效抓取** = 真正进入了补退选页面、并读取了至少一页文本的抓取尝试
  （见 FetchResult.warning_checked）。登录失败 / Chrome 启动失败 / opencli 异常等
  没有机会观察风控警告的情况**不计入分母**，避免人为稀释风险率。
- 重试的每个 attempt 独立计数（见 runner._record_sample）。
- 历史持久化到 data/risk_history.json（保留最近 RISK_STORE_MAX=20 条）；
  UI / 调度只统计最近 RISK_WINDOW=10 条，这样以后想改窗口不用从零积累。

label: 0% 无 / 1–10% 低 / 11–30% 中 / 31–60% 高 / >60% 极高
"""

from __future__ import annotations

import json
import re
import time
from collections import deque
from pathlib import Path
from typing import Optional

# 页面中出现的风控/反自动化提示语
WARNING_PATTERN = re.compile(r"刷课机|过于频繁|频率过高|操作频繁|风控|异常访问|请勿使用|禁止自动化|垃圾请求")

RISK_STORE_MAX = 20   # 磁盘保留的样本条数
RISK_WINDOW = 10      # 统计 / 展示使用的最近窗口


def has_warning_text(text: str) -> bool:
    return bool(text and WARNING_PATTERN.search(text))


def risk_label(percent: int) -> str:
    """按触发率分档：0 无 / 1–10 低 / 11–30 中 / 31–60 高 / >60 极高。"""
    if percent <= 0:
        return "无"
    if percent <= 10:
        return "低"
    if percent <= 30:
        return "中"
    if percent <= 60:
        return "高"
    return "极高"


class RiskHistory:
    """实际刷课机警告事件的 rolling rate；历史持久化，进程重启不丢。

    samples: deque[{"ts": float, "hit": bool}]，最多保留 maxlen 条。
    只记录「有效抓取」（有观察机会的 attempt）。
    """

    def __init__(self, path: Optional[Path | str] = None,
                 maxlen: int = RISK_STORE_MAX):
        self.path = Path(path) if path else None
        self.maxlen = maxlen
        self.samples: deque[dict] = deque()
        self._load()

    # -- 持久化 ---------------------------------------------------------
    def _load(self) -> None:
        if not self.path:
            return
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.samples = deque(list(raw.get("samples", []))[-self.maxlen:])
        except Exception:
            self.samples = deque()

    def _save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps({"samples": list(self.samples)},
                           ensure_ascii=False, indent=2),
                encoding="utf-8")
        except Exception:
            pass

    # -- 记录 -----------------------------------------------------------
    def record(self, warning_hit: bool, ts: Optional[float] = None) -> None:
        """记录一次有效抓取是否触发警告。"""
        self.samples.append({"ts": ts or time.time(), "hit": bool(warning_hit)})
        while len(self.samples) > self.maxlen:
            self.samples.popleft()
        self._save()

    # -- 评估（最近窗口） -----------------------------------------------
    def evaluate(self, window: int = RISK_WINDOW) -> tuple[Optional[int], int, int]:
        """返回 (percent:int|None, hits:int, total:int)。

        total = 最近窗口里的有效样本数（可能 < window，如刚启动）；
        无样本 → (None, 0, 0)。
        """
        recent = list(self.samples)[-window:]
        if not recent:
            return None, 0, 0
        hits = sum(1 for s in recent if s["hit"])
        total = len(recent)
        return round(hits * 100 / total), hits, total