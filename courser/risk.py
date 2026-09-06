"""刷课机警告触发率（风控风险评估）。

无法获知选课系统内部的真实风控阈值，这里提供一个**本地启发式估算**：

- 若抓取过程中在页面文本里检测到风控/警告提示语（如「请勿使用刷课机」「过于频繁」等），
  直接判定 100%（已触发/疑似）；
- 否则按「请求频率」估算：请求数 ≈ 登录/登出等固定操作数 + 翻页数，
  除以本轮耗时得到 请求/分钟；频率越高、得分越高；
- 距离上一轮越久，得分按时间衰减（节奏放慢后风险自然回落）。

label: 低 <20 / 中 <50 / 高 <80 / 极高 >=80
"""

from __future__ import annotations

import re
import time

# 页面中出现的风控/反自动化提示语
WARNING_PATTERN = re.compile(r"刷课机|过于频繁|频率过高|操作频繁|风控|异常访问|请勿使用|禁止自动化|垃圾请求")

_FIXED_REQUESTS = 4        # 登出×2 + 打开登录页 + 点击登录 ≈ 固定请求数
_RPM_STEPS = [
    (20.0, 100),   # >20 请求/分钟 → 100
    (12.0, 75),    # >12 → 75
    (8.0, 45),     # >8  → 45
    (5.0, 20),     # >5  → 20
    (0.0, 5),      # 其余 → 5
]
_DECAY_HOURS = 2.0           # 超过这么久没新请求，风险减半
_STALE_SECONDS = 3600        # 一小时内无新评估，显示归零


def has_warning_text(text: str) -> bool:
    return bool(text and WARNING_PATTERN.search(text))


class BotRisk:
    def __init__(self) -> None:
        self._warning_hit = False
        self._last_eval_ts = 0.0
        self._last_score = 0
        self._last_rpm = 0.0

    @property
    def warning_hit(self) -> bool:
        return self._warning_hit

    def mark_warning(self) -> None:
        self._warning_hit = True

    def evaluate(self, pages: int, duration_s: float) -> tuple[int, str]:
        now = time.time()
        if self._warning_hit:
            self._last_eval_ts = now
            self._last_score = 100
            return 100, "已触发/疑似"

        minutes = max(duration_s / 60.0, 0.1)
        rpm = (pages + _FIXED_REQUESTS) / minutes
        score = 5
        for thr, pts in _RPM_STEPS:
            if rpm > thr:
                score = pts
                break

        # 时间衰减：与上一次评估间隔越久，风险越低
        if self._last_eval_ts and self._last_score:
            gap_h = (now - self._last_eval_ts) / 3600.0
            if gap_h > _DECAY_HOURS:
                score = min(score, int(self._last_score * 0.5))
        if now - self._last_eval_ts > _STALE_SECONDS and self._last_eval_ts:
            score = 0

        self._last_eval_ts = now
        self._last_score = score
        self._last_rpm = rpm
        return score, _label(score)

    def describe(self) -> str:
        return f"{self._last_score}%（{_label(self._last_score)}）"


def _label(score: int) -> str:
    if score <= 0:
        return "无"
    if score < 20:
        return "低"
    if score < 50:
        return "中"
    if score < 80:
        return "高"
    return "极高"