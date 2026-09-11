"""底部活动状态栏（• 状态 …）回归测试（无头）。

只报「上一轮结果」状态与下一轮倒计时；页数/课程数等规模信息归顶部 Hero，
底部绝不重复。状态语义：
  监控中(绿) / 成功抓取(绿) / 触发风控(红) / 达到重试上限(红) /
  抓取失败(红) / 本轮已停止(黄) / 未开始(灰)

用法：uv run python tests/tui/test_runstate.py
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
from courser.tui.app import CourserApp  # noqa: E402


class _FakeWatcher:
    """够 _activity_steady / _next_round_text 用的最小 watcher 表面。"""

    def __init__(self, *, running=False, last=None, next_ts=None):
        self.running = running
        self.last_result = last
        self.next_round_ts = next_ts
        self.current_round_started_at = None


def _line(app, w) -> str:
    return app._activity_steady(w)


def test_runstate_status_words():
    app = CourserApp(Config())

    ok = RoundResult(ok=True, pages=3, total=40)
    assert "成功抓取" in _line(app, _FakeWatcher(last=ok)), "上一轮成功 → 绿色成功抓取"

    fail = RoundResult(ok=False, error="网络抖了一下")
    assert "抓取失败" in _line(app, _FakeWatcher(last=fail)), "普通失败 → 抓取失败"

    risk = RoundResult(ok=True, warning_hit=True)
    assert "触发风控" in _line(app, _FakeWatcher(last=risk)), "风控命中 → 触发风控"

    retry = RoundResult(ok=False, retry_exhausted=True, error="x")
    assert "达到重试上限" in _line(app, _FakeWatcher(last=retry)), "重试达上限 → 红色提示"

    from courser.models import FetchFailureKind
    bu = RoundResult(ok=False, failure_kind=FetchFailureKind.BROWSER_UNAVAILABLE)
    assert "浏览器不可用" in _line(app, _FakeWatcher(last=bu)), "浏览器桥不可用 → 红色提示"

    cancelled = RoundResult(ok=False, cancelled=True)
    assert "本轮已停止" in _line(app, _FakeWatcher(last=cancelled)), "用户停止 → 黄色"

    assert "监控中" in _line(app, _FakeWatcher(running=True, last=fail)), "运行中 → 监控中"
    assert "未开始" in _line(app, _FakeWatcher()), "无任何记录 → 未开始"

    # 底部只报状态，绝不重复页数/课程数（Hero 负责）
    out = _line(app, _FakeWatcher(last=ok))
    assert "3 页" not in out and "40" not in out, f"底栏不应带规模信息：{out!r}"
    print("✓ 底部状态：成功抓取/触发风控/达到重试上限/抓取失败/已停止/监控中/未开始；不含页数数据")


def main() -> int:
    test_runstate_status_words()
    print("=" * 60)
    print("底部活动状态栏测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())