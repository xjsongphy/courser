"""调度等待时间回归测试：风控放慢**绝不累积**（防指数爆炸）。

不变式：warning 轮 wait ∈ [0.7×4×base, 1.3×4×base]（固定 4 倍，不随连续命中
叠加）；正常轮 wait ∈ [0.7×base, 1.3×base]。连续两轮命中与单轮命中范围相同。

用法：uv run python tests/unit/test_scheduler_wait.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.config import Config  # noqa: E402
from courser.models import RoundResult  # noqa: E402
from courser.scheduler import MonitorScheduler  # noqa: E402


def _sched() -> MonitorScheduler:
    cfg = Config.load()
    cfg.interval_min = 1.0          # base = 60s
    cfg.interval_jitter = 0.3
    return MonitorScheduler(cfg, log=lambda m: None)


def test_warning_slowdown_never_accumulates():
    s = _sched()
    base = 60.0
    warn = RoundResult(warning_hit=True)
    normal = RoundResult()

    lo4, hi4 = base * 4 * 0.7, base * 4 * 1.3          # 单轮预警的范围
    lo1, hi1 = base * 0.7, base * 1.3                   # 正常轮范围

    for _ in range(30):
        w1 = s._next_wait(warn)
        assert lo4 <= w1 <= hi4, f"预警轮应在 2.8~5.2 倍区间：{w1:.1f}s"
        n1 = s._next_wait(normal)
        assert lo1 <= n1 <= hi1, f"正常轮应在基准区间：{n1:.1f}s"

    # 连续两轮命中风控：范围与单轮完全一致（不把上一次 wait 再放 4 倍）
    prev = s._next_wait(warn)
    again = s._next_wait(warn)
    assert lo4 <= prev <= hi4 and lo4 <= again <= hi4, \
        f"连续命中也不应累积：{prev:.1f}s / {again:.1f}s"
    print("✓ 风控放慢：固定 4 倍基准，连续命中不累积（无指数爆炸）")


def main() -> int:
    test_warning_slowdown_never_accumulates()
    print("=" * 60)
    print("调度等待测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())