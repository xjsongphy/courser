"""文本选择 + 自动复制的回归测试（courser 自接管鼠标选择）。

产品约束：courser 不再依赖终端原生 selection。`run(mouse=True)` 后由 Textual 接管
鼠标——拖动 = 选择、松手（TextSelected）= 自动把选中纯文本经 OSC 52 复制到剪贴板
并底部 toast 提示；滚轮滚 viewport 不改当前光标/selection。要保证的几组不变量：

1. 入口必须是 mouse=True（没有 mouse=False）；
2. 旧的 terminal-native 架构（PrimaryScreenDriver / terminal_state）已彻底移除；
3. 拖动能形成 Textual selection，且 get_selected_text() 能取出纯文本；
4. TextSelected 后自动 copy（非空 selection）；
5. 空 selection 不复制；
6. 自动复制不改变当前页面 / 光标 / 滚动状态；
7. 滚轮滚动与 ↑↓ 移动彼此独立（滚不改变 selection，↑↓ 在可视区内不动滚动条）。

用法：uv run python tests/tui/test_text_selection.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_tmpdir = tempfile.mkdtemp(prefix="courser-tsel-")
os.environ["COURSER_CONFIG"] = str(Path(_tmpdir) / "config.json")

from textual import events  # noqa: E402
from textual.widgets import Static  # noqa: E402

from courser.config import Config  # noqa: E402
from courser.models import Course  # noqa: E402
from courser.tui.app import CourserApp  # noqa: E402


def _mk_courses():
    return [
        Course(course_no="001", name="大学英语四", category="通识课", dept="外国语学院",
               teacher="王", quota=50, selected=40, avail=10, status="可申请",
               seats_raw="50/40", seq="a", page=1),
        Course(course_no="002", name="普通物理", category="专业必修", dept="物理学院",
               teacher="李", quota=60, selected=60, avail=0, status="不可申请",
               seats_raw="60/60", seq="b", page=2),
    ]


def _fresh(cfg=None):
    cfg = cfg or Config.load()
    cfg.first_run_done = True
    cfg.notify.to = "x@y.z"
    return CourserApp(cfg)


async def _drag(pilot, p1=(10, 12), p2=(50, 12)) -> None:
    await pilot.mouse_down(offset=p1)
    await pilot.mouse_up(offset=p2)


# ---------------------------------------------------------------------------
# 结构守卫：入口与旧架构移除
# ---------------------------------------------------------------------------
def test_run_uses_mouse_true() -> None:
    """入口必须显式 mouse=True（Textual 接管鼠标选择），不得再出现 mouse=False。"""
    src = (Path(__file__).resolve().parents[2]
           / "courser" / "tui" / "app.py").read_text(encoding="utf-8")
    assert re.search(r"\.run\(\s*mouse=True\s*\)", src), "入口应保持 run(mouse=True)"
    assert "mouse=False" not in src, "旧 mouse=False 已废弃，不应残留"


def test_legacy_native_selection_removed() -> None:
    """旧 terminal-native 架构必须彻底移除（无 fallback，单一路径）。"""
    root = Path(__file__).resolve().parents[2]
    assert not (root / "courser" / "tui" / "driver.py").exists(), "driver.py 已废弃"
    assert not (root / "courser" / "tui" / "terminal_state.py").exists(), \
        "terminal_state.py 已废弃"
    src = (root / "courser" / "tui" / "app.py").read_text(encoding="utf-8")
    for token in ("PrimaryScreenDriver", "get_driver_class", "disable_terminal_mouse",
                  "strip_alt_screen"):
        assert token not in src, f"{token} 已废弃，不应残留"
    assert not (root / "tests" / "tui" / "test_terminal_selection.py").exists(), \
        "旧测试文件已废弃"


# ---------------------------------------------------------------------------
# 拖动选择 + 自动复制
# ---------------------------------------------------------------------------
async def test_drag_forms_selection() -> None:
    """拖动能形成 Textual selection，且能取出纯文本。"""
    app = _fresh()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.courses = _mk_courses()
        app._render_main(force=True)
        await pilot.pause(0.2)
        await _drag(pilot)
        await pilot.pause(0.1)
        text = app.screen.get_selected_text()
        assert text, "拖动应形成非空 selection"
    print("✓ 拖动形成 Textual selection")


async def test_text_selected_auto_copies() -> None:
    """MouseUp 形成的 selection 应自动复制到剪贴板，并 toast 提示（只一次）。"""
    app = _fresh()
    app.copy_to_clipboard = mock.Mock()
    app.notify = mock.Mock()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.courses = _mk_courses()
        app._render_main(force=True)
        await pilot.pause(0.2)
        await _drag(pilot)
        await pilot.pause(0.2)
        text = app.screen.get_selected_text()
        assert text
        app.copy_to_clipboard.assert_called_once_with(text)
        assert app.notify.call_count >= 1, "复制后应有 toast 提示"
    print("✓ TextSelected → 自动复制 + toast")


async def test_empty_selection_does_not_copy() -> None:
    """空 selection 不触发复制（handler 入口即守卫）。"""
    app = _fresh()
    app.copy_to_clipboard = mock.Mock()
    app.notify = mock.Mock()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        # 清掉 selection → get_selected_text() 返回空
        app.screen.clear_selection()
        app._auto_copy_selection(events.TextSelected())
        await pilot.pause(0.1)
        app.copy_to_clipboard.assert_not_called()
        app.notify.assert_not_called()
    print("✓ 空 selection 不复制")


async def test_auto_copy_does_not_change_state() -> None:
    """自动复制不应改变当前页面 / 光标 / 滚动位置。"""
    app = _fresh()
    app.copy_to_clipboard = mock.Mock()
    async with app.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.3)
        app.courses = _mk_courses()
        app._render_main(force=True)
        await pilot.pause(0.1)
        before = (app.page, app.main.index, app.main.top, app.main.view)
        await _drag(pilot)
        await pilot.pause(0.2)
        after = (app.page, app.main.index, app.main.top, app.main.view)
        assert before == after, f"自动复制不应改变页面/光标，{before} → {after}"
    print("✓ 自动复制不改页面/光标/滚动")


# ---------------------------------------------------------------------------
# 滚轮与 selection 解耦
# ---------------------------------------------------------------------------
async def test_settings_wheel_does_not_change_selection() -> None:
    """设置页滚轮只滚 viewport，不改当前 selection（各字段选择 sv.index）。"""
    app = _fresh()
    async with app.run_test(size=(100, 22)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("s")
        await pilot.pause(0.2)
        assert app.page == "settings"
        sc = app.query_one("#settingsscroll")
        assert sc.max_scroll_y > 0, "设置页应有可滚动内容"
        idx_before = app.sv.index
        y_before = sc.scroll_y
        sc.post_message(events.MouseScrollDown(
            widget=sc, x=5, y=5, delta_x=0, delta_y=1, button=4,
            shift=False, meta=False, ctrl=False, screen_x=5, screen_y=5))
        await pilot.pause(0.2)
        assert sc.scroll_y > y_before, "滚轮应滚动 viewport"
        assert app.sv.index == idx_before, "滚轮滚动不应改变 selection"
    print("✓ 设置页滚轮只滚 viewport，不改 selection")


async def test_settings_arrow_scroll_minimal_follow():
    """设置页 ↑↓：可视区内移动不改滚动条，越界才最小跟随（不钉顶）。
    回归：坐标统一走 scroll_to_region + scrollable_content_region，避免 off-by-N。"""
    app = _fresh()
    async with app.run_test(size=(100, 22)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("s")
        await pilot.pause(0.2)
        sc = app.query_one("#settingsscroll")
        assert sc.scrollable_content_region.height > 0, "滚动容器应有真实 viewport"
        s0 = float(sc.scroll_y)
        # 可视区内往下几行：滚动条不动
        for _ in range(4):
            await pilot.press("down")
            await pilot.pause(0.03)
        assert float(sc.scroll_y) == s0, "可视区内 ↑↓ 不应移动滚动条"
        # 跳到末尾：最小必要跟随（光标保持可见）
        for _ in range(len(app.s_rows) + 2):
            await pilot.press("down")
            await pilot.pause(0.02)
        assert float(sc.scroll_y) == float(sc.max_scroll_y), "越界应最小跟随到末尾"
    print("✓ 设置页 ↑↓ 最小跟随：可视区内不动，越界才滚")


async def test_settings_top_header_reachable_and_no_drift():
    """设置页滚动：从底部回顶时「账号」组头（正文第 0 行）可复现；编辑/连按 Tab
    不改变滚动位置；内部 sv.scroll 与真实 scroll_y 全程一致（抗漂移）。
    回归：上滚曾把光标钉在顶边使 scroll 到不了 0，组头/备注永远滚不出来。"""
    app = _fresh()
    async with app.run_test(size=(110, 26)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("s")
        await pilot.pause(0.2)
        sc = app.query_one("#settingsscroll")
        vh = app._settings_viewport_height()
        txt = app.query_one("#settings_list")
        # 下到底
        for _ in range(25):
            await pilot.press("down")
            await pilot.pause(0.015)
        # 回顶：光标每一步都在视口内、内部与真实一致
        for _ in range(25):
            y = app.sv.row_y.get(app.sv.index)
            top = int(sc.scroll_y or 0)
            assert top <= y <= top + vh - 1, "回顶时光标不能越出视口"
            assert app.sv.scroll == int(sc.scroll_y or 0), "内部计数与真实滚动应一致"
            await pilot.press("up")
            await pilot.pause(0.015)
        assert app.sv.index == 0
        top = int(sc.scroll_y or 0)
        assert top == 0, "回顶应回到正文顶部（露出组头）"
        lines = str(txt.render()).splitlines()
        assert lines and "账号" in lines[top], f"可见首行应为账号组头，实际 {lines[top] if top < len(lines) else 'N/A'!r}"
        # 编辑一个文本字段并回车确认：滚动位置与光标可见性不变
        to = next(i for i, (_g, f) in enumerate(app.s_rows) if f["key"] == "to")
        app.sv.index = to
        app._render_settings_list()
        await pilot.pause(0.05)
        top0 = float(sc.scroll_y)
        await pilot.press("enter")
        await pilot.pause(0.05)
        await pilot.press("z")
        await pilot.pause(0.03)
        await pilot.press("enter")
        await pilot.pause(0.15)
        await pilot.pause(0.2)
        assert float(sc.scroll_y) == top0, "编辑确认不应改变滚动位置"
        assert app.sv.scroll == int(sc.scroll_y or 0)
        assert top0 <= app.sv.row_y.get(app.sv.index) <= top0 + vh - 1, "编辑后光标应仍在视口内"
        # 枚举连按两下 Tab：值循环回原值，滚动不动
        fr = next(i for i, (_g, f) in enumerate(app.s_rows) if f["key"] == "force_relogin")
        app.sv.index = fr
        app._render_settings_list()
        await pilot.pause(0.05)
        val0 = app.sd["force_relogin"]
        top0 = float(sc.scroll_y)
        await pilot.press("tab")
        await pilot.pause(0.03)
        await pilot.press("tab")
        await pilot.pause(0.05)
        assert app.sd["force_relogin"] == val0, "连按两下 Tab 值应循环回原值"
        assert float(sc.scroll_y) == top0, "连按 Tab 不应改变滚动位置"
        assert app.sv.scroll == int(sc.scroll_y or 0)
    print("✓ 设置页顶组头可复现 + 编辑/Tab 不漂移滚动")


async def test_filters_arrow_scroll_minimal_follow():
    """筛选页也必须使用真实 viewport，光标不能随列表裁剪跑出页面。"""
    app = _fresh()
    app.candidate_lists["names"] = [f"候选课程 {i:02d}" for i in range(50)]
    async with app.run_test(size=(100, 22)) as pilot:
        await pilot.pause(0.3)
        await pilot.press("f")
        await pilot.pause(0.2)
        sc = app.query_one("#filtersscroll")
        assert sc.max_scroll_y > 0, "筛选候选过多时列表应独立滚动"
        y0 = float(sc.scroll_y)
        for _ in range(4):
            await pilot.press("down")
            await pilot.pause(0.03)
        assert float(sc.scroll_y) == y0, "可视区内移动不应滚动筛选列表"
        for _ in range(60):
            await pilot.press("down")
            await pilot.pause(0.01)
        assert app.fv.index == 49, "光标应停在最后一项，而非越出列表"
        assert float(sc.scroll_y) == float(sc.max_scroll_y), \
            "光标到末尾时筛选列表应最小跟随到底部"

        # 重进筛选页会重置到 SEARCH/index=0；不能保留刚才的物理滚动位置，
        # 否则逻辑焦点和 viewport 脱节，❯ 会被裁到屏幕外。
        app._begin_filters()
        await pilot.pause(0.1)
        assert app.fv.focus == "search" and app.fv.index == 0
        assert float(sc.scroll_y) == 0.0, "重置筛选焦点时应同步回到候选顶部"
    print("✓ 筛选页 ↑↓ 最小跟随：光标始终在真实 viewport 内")


async def test_help_logs_detail_have_wheel_scrollable_view() -> None:
    """帮助 / 日志 / 详情页都是可滚 FocusScroll（mouse=True 下可滚轮滚动）。"""
    app = _fresh()
    async with app.run_test(size=(100, 26)) as pilot:
        await pilot.pause(0.3)
        # 详情：先有课程 + 进入详情
        app.courses = _mk_courses()
        app._render_main(force=True)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert app.page == "detail"
        dsc = app.query_one("#detscroll")
        assert dsc.max_scroll_y > 0, "详情页应可滚动"
        # 日志
        for _ in range(40):
            app.log_line("日志行日志行日志行日志行日志行")
        await pilot.press("escape")
        await pilot.press("l")
        await pilot.pause(0.2)
        lsc = app.query_one("#logscroll")
        assert lsc.max_scroll_y > 0, "日志页应可滚动"
        # 帮助
        await pilot.press("escape")
        await pilot.press("h")
        await pilot.pause(0.2)
        hsc = app.query_one("#helpscroll")
        assert hsc.max_scroll_y > 0, "帮助页应可滚动"
    print("✓ 详情 / 日志 / 帮助页均可滚动（滚轮路径可用）")


async def main() -> int:
    test_run_uses_mouse_true()
    test_legacy_native_selection_removed()
    await test_drag_forms_selection()
    await test_text_selected_auto_copies()
    await test_empty_selection_does_not_copy()
    await test_auto_copy_does_not_change_state()
    await test_settings_wheel_does_not_change_selection()
    await test_settings_arrow_scroll_minimal_follow()
    await test_settings_top_header_reachable_and_no_drift()
    await test_filters_arrow_scroll_minimal_follow()
    await test_help_logs_detail_have_wheel_scrollable_view()
    print("文本选择 + 自动复制结构/行为检查 OK：mouse=True · 拖动选择 · 自动复制 · 空选区忽略 · 不改变状态 · 滚轮解耦")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
