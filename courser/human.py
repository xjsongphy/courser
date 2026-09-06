"""人类节奏模拟：相邻操作之间随机间隔。

所有 sleep 都使用随机区间，避免固定间隔暴露自动化痕迹；
同时保持低频（间隔较短的操作按秒计，轮询按分钟计）。
"""

from __future__ import annotations

import random
import time

__all__ = ["sleep_rand", "jitter", "human_delay_range"]


def sleep_rand(lo: float, hi: float) -> float:
    """随机休眠 [lo, hi] 秒并返回实际休眠时长。"""
    if hi < lo:
        lo, hi = hi, lo
    secs = random.uniform(lo, hi)
    time.sleep(secs)
    return secs


def jitter(value: float, ratio: float = 0.3) -> float:
    """在 value*(1-ratio) ~ value*(1+ratio) 之间随机抖动（用于轮询间隔，分钟级）。"""
    return value * random.uniform(1.0 - ratio, 1.0 + ratio)


def human_delay_range(base_lo: float = 6.0, base_hi: float = 14.0) -> tuple[float, float]:
    """两次页面操作之间的随机间隔范围（秒）：在基准区间上再做一次随机缩放。"""
    lo = base_lo * random.uniform(0.8, 1.2)
    hi = base_hi * random.uniform(0.9, 1.3)
    return (min(lo, hi), max(lo, hi))