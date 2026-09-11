"""调度器「夜间暂停」测试：0:00~8:00 暂停抓取，8 点后按 0 点前状态恢复。

覆盖：
- _in_night_window 边界（0/7 在窗内，8 出窗）
- 跨夜运行（0 点前在运行）→ 8 点恢复抓取
- 0 点前未运行（夜间中途启动）→ 8 点不自动恢复、保持暂停
- 夜间 window 内绝不执行 run_round
- night_pause_active / night_pause 持久化

用法：uv run python tests/unit/test_scheduler_night.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")
# _loop 每轮会 cfg.reload()（重读磁盘），因此测试要把 night_pause 持久化到
# COURSER_CONFIG 指向的文件，reload 后仍为开启，否则会被默认值冲回 False。
Path(os.environ["COURSER_CONFIG"]).write_text(
    '{"night_pause": true, "interval_min": 8.0}', encoding="utf-8")

import courser.scheduler as sched_mod  # noqa: E402
from courser.config import Config  # noqa: E402
from courser.models import FetchResult  # noqa: E402
from courser.scheduler import MonitorScheduler, _in_night_window  # noqa: E402


def _mk_sched(cfg, calls):
    def fake_fetch(**k):
        calls.append(1)
        fr = FetchResult(login_mode="reuse_session", pages=1, total=20, ok=True)
        fr.courses = []
        return fr
    s = MonitorScheduler(cfg, log=lambda m: None)
    s._fetch_round = fake_fetch
    return s


def _spawn_loop(s, started_in_night: bool):
    """直接在当前线程外跑 _loop，便于精确控制 started_in_night 而不走 start()。"""
    s._started_in_night = started_in_night
    s.running = True
    s._stop.clear()
    t = threading.Thread(target=s._loop, daemon=True)
    t.start()
    return t


def test_night_window_boundary():
    assert _in_night_window(time.struct_time((2026, 1, 1, 0, 0, 0, 0, 0, -1)))
    assert _in_night_window(time.struct_time((2026, 1, 1, 5, 59, 59, 0, 0, -1)))
    assert not _in_night_window(time.struct_time((2026, 1, 1, 6, 0, 0, 0, 0, -1)))
    # 关键：下午 12 点（正午）与 20 点绝不能被算成夜间窗口
    assert not _in_night_window(time.struct_time((2026, 1, 1, 12, 0, 0, 0, 0, -1)))
    assert not _in_night_window(time.struct_time((2026, 1, 1, 20, 0, 0, 0, 0, -1)))
    assert not _in_night_window(time.struct_time((2026, 1, 1, 23, 59, 0, 0, 0, -1)))
    print("✓ _in_night_window 边界：0/5 在窗内、6/12/20/23 出窗")


def test_config_persists_night_pause():
    p = Path(tempfile.mkdtemp()) / "config.json"
    c = Config(); c.night_pause = True; c.save(p)
    assert Config.load(p).night_pause is True
    print("✓ night_pause 持久化 round-trip")


def test_resume_after_morning_when_running_before_0():
    """0 点前在运行（白天启动）：夜间暂停，8 点后恢复抓取。"""
    in_night = [True]
    orig = sched_mod._in_night_window
    sched_mod._in_night_window = lambda: in_night[0]
    calls = []
    try:
        cfg = Config(); cfg.night_pause = True
        s = _mk_sched(cfg, calls)
        # 8 点一到达、下一次判断即出窗
        s._suppress_until_morning = lambda: (in_night.__setitem__(0, False), False)[1]
        t = _spawn_loop(s, started_in_night=False)
        time.sleep(0.35)
        s.request_stop()
        t.join(timeout=2)
        assert not t.is_alive()
        assert len(calls) >= 1, f"跨夜运行 8 点后应恢复抓取，实际 {calls}"
    finally:
        sched_mod._in_night_window = orig
    print("✓ 跨夜运行 → 8 点后恢复抓取")


def test_do_not_autorun_when_off_before_0():
    """0 点前未运行（夜间中途启动）：夜间不抓取，8 点后也不自动恢复、保持暂停。"""
    in_night = [True]
    orig = sched_mod._in_night_window
    sched_mod._in_night_window = lambda: in_night[0]
    calls = []
    try:
        cfg = Config(); cfg.night_pause = True
        s = _mk_sched(cfg, calls)
        # 8 点到达后出窗，但 0 点前未运行 → 循环结束且不抓取
        s._suppress_until_morning = lambda: (in_night.__setitem__(0, False), False)[1]
        t = _spawn_loop(s, started_in_night=True)
        time.sleep(0.35)
        s.request_stop()
        t.join(timeout=2)
        assert not t.is_alive()
        assert calls == [], f"0 点前未运行 → 8 点后不应自动抓取，实际 {calls}"
        assert s.running is False, "循环应结束、保持暂停（running=False）"
    finally:
        sched_mod._in_night_window = orig
    print("✓ 0 点前未运行 → 夜间不抓取、8 点后保持暂停不自动恢复")


def test_no_rounds_during_night_even_if_running():
    """夜间 window 内：即使跨夜运行，也不执行任何 run_round。"""
    in_night = [True]
    orig = sched_mod._in_night_window
    sched_mod._in_night_window = lambda: in_night[0]
    calls = []
    night_calls = []
    try:
        cfg = Config(); cfg.night_pause = True
        s = _mk_sched(cfg, calls)
        # 一直保持夜间：_suppress_until_morning 不退出，循环不该抓取
        s._suppress_until_morning = lambda: (night_calls.append(1) or time.sleep(0.05) or False)
        t = _spawn_loop(s, started_in_night=False)
        time.sleep(0.3)
        s.request_stop()
        t.join(timeout=2)
        assert calls == [], f"夜间 window 内不应执行 run_round，实际 {calls}"
        assert night_calls, "夜间应在等待循环里"
    finally:
        sched_mod._in_night_window = orig
    print("✓ 夜间 window 内不执行任何轮次（只在等待循环）")


def test_night_pause_active_property():
    cfg = Config(); cfg.night_pause = True
    s = MonitorScheduler(cfg, log=lambda m: None)
    s.running = True
    orig = sched_mod._in_night_window
    try:
        sched_mod._in_night_window = lambda: True
        assert s.night_pause_active is True
        assert s.night_resume_ts is not None
        sched_mod._in_night_window = lambda: False
        assert s.night_pause_active is False
        assert s.night_resume_ts is None
    finally:
        sched_mod._in_night_window = orig
    print("✓ night_pause_active / night_resume_ts")


def main() -> int:
    test_night_window_boundary()
    test_config_persists_night_pause()
    test_resume_after_morning_when_running_before_0()
    test_do_not_autorun_when_off_before_0()
    test_no_rounds_during_night_even_if_running()
    test_night_pause_active_property()
    print("=" * 60)
    print("调度器夜间暂停测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())