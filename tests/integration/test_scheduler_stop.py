"""监控调度回归测试：BROWSER_UNAVAILABLE 时整个监控线程停止，不再计划下一轮。

与之对比：普通临时错误（BROWSER_ERROR）最多原地重试一次后下一周期继续，
但 BROWSER_UNAVAILABLE 是结构性环境故障——再等下一轮只会重复慢登录/超时，
必须停止并提示人工修环境。

用法：uv run python tests/integration/test_scheduler_stop.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.config import Config  # noqa: E402
from courser.models import FetchFailureKind, FetchResult  # noqa: E402
from courser.scheduler import MonitorScheduler  # noqa: E402


def _browser_unavailable_fetch(**_k):
    fr = FetchResult(ok=False, error="OpenCLI/Chrome 桥不可达")
    fr.failure_kind = FetchFailureKind.BROWSER_UNAVAILABLE
    return fr


def test_monitor_stops_on_browser_unavailable():
    cfg = Config.load()
    cfg.interval_min = 0.01          # 若不停机会疯狂轮询，立刻被发现
    logs: list[str] = []
    sched = MonitorScheduler(cfg, log=logs.append,
                             fetch_round=_browser_unavailable_fetch)
    sched.start()
    deadline = time.time() + 6
    while sched.running and time.time() < deadline:
        time.sleep(0.05)
    assert not sched.running, "BROWSER_UNAVAILABLE 应让监控线程停止而非继续轮询"
    assert sched.next_round_ts is None, "停止后不应再安排下一轮"
    assert any("停止监控" in m for m in logs), f"应有明确停止日志：{logs}"
    assert any("浏览器桥不可用" in m for m in logs), logs
    print("✓ BROWSER_UNAVAILABLE → 监控线程停止、不再排下一轮、明确提示修环境")


def main() -> int:
    test_monitor_stops_on_browser_unavailable()
    print("=" * 60)
    print("监控停止调度测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())