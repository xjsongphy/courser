"""调度器「随机暂停」测试：档位（关/轻/中/强）→ 连续工作轮数 / 暂停时长。

档位映射见 config.RANDOM_BREAK_PROFILES。工作窗口模型（非概率）保证不会连续休息。
覆盖：档位映射、成功计数触发/重置、失败取消清零、关闭不触发、换档重开计数、
config 持久化与旧 bool 兼容。

用法：uv run python tests/unit/test_scheduler_random_break.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")
# _loop 里 run_round 会 cfg.reload()（重读磁盘），循环测试要求档位已持久化。
Path(os.environ["COURSER_CONFIG"]).write_text('{"random_break": "medium"}', encoding="utf-8")

from courser.config import Config, RANDOM_BREAK_PROFILES  # noqa: E402
from courser.models import FetchResult  # noqa: E402
from courser.scheduler import MonitorScheduler  # noqa: E402


def _ok():
    return FetchResult(login_mode="reuse_session", pages=1, ok=True, cancelled=False)


def test_profiles_shape():
    assert set(RANDOM_BREAK_PROFILES) == {"light", "medium", "strong"}
    expect = {"light": ((5, 8), (5, 12)), "medium": ((3, 6), (8, 20)),
              "strong": ((2, 4), (12, 30))}
    assert RANDOM_BREAK_PROFILES == expect
    for (r0, r1), (d0, d1) in RANDOM_BREAK_PROFILES.values():
        assert r0 <= r1 and d0 < d1
    print("✓ 三档映射正确（轻 5~8/5~12、中 3~6/8~20、强 2~4/12~30）")


def test_config_persists_tier_and_bool_compat():
    p = Path(tempfile.mkdtemp()) / "config.json"
    c = Config(); c.random_break = "strong"; c.save(p)
    assert Config.load(p).random_break == "strong"
    # 旧 bool 写法兼容：true → medium
    p.write_text('{"random_break": true}', encoding="utf-8")
    assert Config.load(p).random_break == "medium"
    print("✓ 档位持久化 + 旧 bool 兼容")


def test_trigger_after_target_and_reset():
    cfg = Config(); cfg.random_break = "strong"   # 2~4 轮
    s = MonitorScheduler(cfg, log=lambda m: None)
    with mock.patch("courser.scheduler.random.randint", return_value=3), \
         mock.patch("courser.scheduler.random.uniform", return_value=15):
        assert s._maybe_break(_ok()) is None      # 第 1 轮（首次开启抽目标=3）
        assert s._maybe_break(_ok()) is None      # 第 2 轮
        secs = s._maybe_break(_ok())              # 第 3 轮 → 触发
    (r0, r1), (d0, d1) = RANDOM_BREAK_PROFILES["strong"]
    assert secs is not None and d0 * 60 <= secs <= d1 * 60
    assert s._success_streak == 0, "休息后计数应重置"
    assert s._break_after == 3
    print("✓ 成功计数达到目标 → 触发休息并重置（时长落在档位区间）")


def test_resets_on_failure_and_cancel():
    cfg = Config(); cfg.random_break = "medium"
    s = MonitorScheduler(cfg, log=lambda m: None)
    with mock.patch("courser.scheduler.random.randint", return_value=6):
        s._maybe_break(_ok())
        assert s._success_streak == 1
        s._maybe_break(FetchResult(ok=False))
        assert s._success_streak == 0
    assert s._maybe_break(FetchResult(ok=True, cancelled=True)) is None
    assert s._success_streak == 0
    print("✓ 失败/取消清零，不触发休息")


def test_off_never_triggers():
    cfg = Config(); cfg.random_break = "off"
    s = MonitorScheduler(cfg, log=lambda m: None)
    assert s._maybe_break(_ok()) is None and s._success_streak == 0
    print("✓ 档位=关 → 永不触发")


def test_tier_change_restarts_count():
    cfg = Config(); cfg.random_break = "medium"
    s = MonitorScheduler(cfg, log=lambda m: None)
    with mock.patch("courser.scheduler.random.randint", return_value=6):
        s._maybe_break(_ok())
        s._maybe_break(_ok())              # streak=2，目标 6
        assert s._success_streak == 2
        cfg.random_break = "strong"        # 换档 → 计数清零、目标重抽
        s._maybe_break(_ok())
    assert s._success_streak == 1, "换档后应从 1 重新计数"
    print("✓ 换档重启计数")


def test_loop_takes_break():
    cfg = Config(); cfg.random_break = "medium"
    s = MonitorScheduler(cfg, log=lambda m: None)
    s._fetch_round = lambda **k: _ok()
    waits = []
    s._wait_sleep = lambda w: waits.append(w)
    s._next_wait = lambda last: 0.01
    s._break_after = 1
    with mock.patch("courser.scheduler.random.randint", return_value=1), \
         mock.patch("courser.scheduler.random.uniform", return_value=15):
        s.running = True
        s._stop.clear()
        t = threading.Thread(target=s._loop, daemon=True)
        t.start()
        time.sleep(0.15)
        s.request_stop()
        t.join(timeout=2)
    assert not t.is_alive()
    d0 = RANDOM_BREAK_PROFILES["medium"][1][0]
    assert any(w >= d0 * 60 for w in waits), f"循环里应出现长休息，waits={waits}"
    print("✓ 循环到达阈值后执行长休息")


def main() -> int:
    test_profiles_shape()
    test_config_persists_tier_and_bool_compat()
    test_trigger_after_target_and_reset()
    test_resets_on_failure_and_cancel()
    test_off_never_triggers()
    test_tier_change_restarts_count()
    test_loop_takes_break()
    print("=" * 60)
    print("调度器随机暂停测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())