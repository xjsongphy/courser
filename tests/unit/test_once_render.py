"""`courser --once` 非 TUI 结果渲染测试（Rich Panel + Table）。

用法：uv run python tests/unit/test_once_render.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from rich.console import Console  # noqa: E402

from courser.models import Course, RoundResult  # noqa: E402
from courser.tui.app import _render_once_result  # noqa: E402


def _render(r: RoundResult, width: int = 120) -> str:
    buf = StringIO()
    out = Console(file=buf, width=width, color_system=None)  # 非 TTY：去色、留框线
    _render_once_result(out, r)
    return buf.getvalue()


def test_once_success_with_matched():
    r = RoundResult(ok=True, pages=8, total=141)
    r.matched = [Course(course_no="02332976", name="《理想国》", category="通识课(通识核心课I)",
                        dept="哲学系", quota=100, selected=100, avail=0,
                        status="不可申请", page=2)]
    s = _render(r)
    assert "补退选空余名额" in s, "应有带标题的汇总 Panel"
    assert "页数 8" in s and "课程 141" in s and "命中 1" in s and "空余 0" in s
    assert "● 成功" in s, "成功状态应显示"
    assert "《理想国》" in s and "02332976" in s, "命中课程应进表格"
    print("✓ --once 成功渲染：Panel/汇总/命中表/成功状态")


def test_once_success_with_seats():
    r = RoundResult(ok=True, pages=3, total=40)
    r.matched = [Course(course_no="02130020", name="中国近代史", category="通识课(通识核心课I)",
                        dept="历史学系", quota=160, selected=159, avail=1,
                        status="可申请", page=3)]
    s = _render(r)
    assert "空余 1" in s, "空余数应体现"
    assert "可申请" in s
    print("✓ --once 有空余：空余计数 + 状态列")


def test_once_no_match_failure():
    s = _render(RoundResult(ok=False, pages=0, total=0))
    assert "● 失败" in s, "失败状态应显示"
    assert "本轮无命中课程" in s, "无命中应有提示而非空表"
    print("✓ --once 无命中失败：失败状态 + 无课程提示")


# ---------------------------------------------------------------------------
# --once 表现层（Live/Plain）
# ---------------------------------------------------------------------------


def _mk_ok() -> RoundResult:
    r = RoundResult(ok=True, login_mode="login_click", pages=7, total=136)
    r.matched = [Course(avail=1), Course(avail=1)] + [Course(avail=0)] * 4
    r.notified = [object()]
    return r


def _render_summary(r) -> str:
    from courser.tui.once_render import render_summary_from_result
    buf = StringIO()
    Console(file=buf, force_terminal=False, width=100, color_system=None).print(
        render_summary_from_result(r))
    return buf.getvalue()


def test_summary_ok():
    s = _render_summary(_mk_ok())
    assert "✓" in s and "登录" in s and "重新登录" in s
    assert "7 页 · 136 门" in s
    assert "6 门 · 2 门有空余" in s
    assert "已发送 1 封" in s
    print("✓ 摘要（成功）：登录/抓取/筛选/通知 ✓ 行")


def test_summary_fail():
    r = RoundResult(ok=False, error="登录未成功。尝试记录：…")
    s = _render_summary(r)
    assert "✗" in s and "本轮失败" in s and "登录未成功" in s
    print("✓ 摘要（失败）：✗ 行与错误首行")


def _render_live(ren) -> str:
    buf = StringIO()
    Console(file=buf, force_terminal=False, width=100, color_system=None).print(ren._render())
    return buf.getvalue()


def test_tty_live_progress_and_phase_commit():
    """TTY：进度驱动 ✓ 阶段行推进 + spinner 当前行；默认静默 debug 日志。"""
    from rich.console import Console
    from courser.tui.once_render import make_once_renderer
    buf = StringIO()
    con = Console(file=buf, force_terminal=True, width=100, highlight=False)
    ren = make_once_renderer(con, con, verbose=False)
    ren.progress(1, None, "检查现有登录状态…")
    ren.progress(2, None, "已复用现有登录会话")
    ren.progress(3, None, "已进入补退选页")
    mid = _render_live(ren)
    assert "登录" in mid and "补退选" in mid, "推进应把已完成阶段留成行"
    ren.progress(4, None, "正在读取课程列表 第 4/7 页")
    live = _render_live(ren)
    assert "正在读取课程列表" in live and "抓取" in live, "当前动作应为 spinner 行"

    ren.log("抓取准备：当前页=1")       # debug → 默认静默
    assert "抓取准备：当前页=1" not in _render_live(ren)
    ren.log("邮件已发送 → x@y.z")      # important → 浮出
    assert "邮件已发送 → x@y.z" in _render_live(ren)

    ren.finish(_mk_ok())
    ren.stop()
    final = buf.getvalue()
    assert "7 页 · 136 门" in final and "邮件已发送 → x@y.z" in final
    print("✓ TTY Live：阶段 ✓ 提交 + spinner + 默认静默 debug / 浮出重要日志")


def test_plain_keeps_line_stream():
    """非 TTY：逐行文本 + [courser] 前缀，保留 debug（grep/cron 友好）。"""
    from rich.console import Console
    from courser.tui.once_render import make_once_renderer
    buf = StringIO()
    con = Console(file=buf, force_terminal=False, width=100)
    ren = make_once_renderer(con, con, verbose=False)
    ren.progress(1, None, "正在读取课程列表 第 4/7 页")
    ren.log("抓取准备：当前页=1")
    ren.finish(_mk_ok())
    ren.stop()
    out = buf.getvalue()
    assert "[courser]" in out and "正在读取课程列表 第 4/7 页" in out
    assert "抓取准备：当前页=1" in out, "非 TTY 保留完整明细以便 grep"
    print("✓ Plain：非 TTY 保留 [courser] 逐行 + 明细")


def main() -> int:
    test_once_success_with_matched()
    test_once_success_with_seats()
    test_once_no_match_failure()
    test_summary_ok()
    test_summary_fail()
    test_tty_live_progress_and_phase_commit()
    test_plain_keeps_line_stream()
    print("=" * 60)
    print("--once 结果渲染测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())