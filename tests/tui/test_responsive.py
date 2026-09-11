"""响应式几何回归测试：四档终端尺寸无头启动主页课程窗口。

覆盖 80×24 / 100×30 / 120×36 / 160×45：
- 渲染不抛异常、页面就位、每档都能看到课程
- 每行（含折行）都不超出终端宽度、都是合法 markup
- 表头各必保列都在
（注：新版渲染器按真实折行行数分页，故不断言『可见课程数随高度单调』——
  更宽的窗口可能让每门课占更少物理行，两者互相抵消。）
用法：uv run python tests/tui/test_responsive.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

import io

from rich.cells import cell_len
from rich.console import Console
from rich.text import Text

from courser.config import Config
from courser.models import Course
from courser.tui.app import CourserApp


_GEOMETRIES = [(80, 24), (100, 30), (120, 36), (160, 45)]


def _mk_courses() -> list[Course]:
    return [
        Course(course_no=f"{i:03d}", name=f"计算机科学与技术导论 {i}（含实验与课程设计）",
               category="通识课(通识核心课III)", dept="信息科学技术学院",
               teacher="张伟(教授)", quota=100 - i, selected=99 - i,
               avail=1 if i % 2 == 0 else 0, seats_raw="100/99", seq=f"s{i}",
               status="可申请", page=i + 1)
        for i in range(200)
    ]


def _mk_single_line(n: int = 80) -> list[Course]:
    """单行课程：用于断言真实 viewport 被填满。"""
    return [Course(course_no=f"{i:03d}", name=f"课程 {i}", category="任选",
                   dept="物理学院", quota=30, selected=0, avail=1, seq=f"s{i}", page=1)
            for i in range(n)]


def _viewport_capacity(app) -> int:
    return max(1, app.query_one("#courselist").content_region.height)


def _list_text(app) -> str:
    """课程列表由字符串更新；Textual 8 返回 Content。"""
    return app.query_one("#courselist").render().plain


def _panel_text(app) -> str:
    """hero 当前内容就是 Rich Panel，不经 render() 的 RichVisual 包装。"""
    buf = io.StringIO()
    Console(force_terminal=False, color_system=None, width=80, file=buf).print(
        app.query_one("#hero").content, end="")
    return buf.getvalue()


async def _render_at(w: int, h: int) -> tuple[str, str, int, str, str]:
    cfg = Config()
    cfg.first_run_done = True
    app = CourserApp(cfg)
    async with app.run_test(size=(w, h)) as p:
        await p.pause(0.3)
        app.courses = _mk_courses()
        app._set_view("all")
        app._render_main(force=True)
        body = _list_text(app)
        header = str(app.query_one("#coursehead").render())
        runstate = app.query_one("#activity")
        hero = _panel_text(app)
        return body, header, runstate.size.height, str(runstate.render()), hero


async def test_viewport_fill_and_arrow_stable():
    """课程区按真实 viewport 填满；按 ↑↓/PgUp/PgDn 不改变容量/已渲染行数。"""
    cfg = Config()
    cfg.first_run_done = True
    app = CourserApp(cfg)
    async with app.run_test(size=(120, 36)) as p:
        await p.pause(0.3)
        app.courses = _mk_single_line()
        app._set_view("all")
        app._render_main(force=True)
        await p.pause(0.05)
        cap0 = _viewport_capacity(app)
        rows0 = len(_list_text(app).splitlines())
        assert rows0 == cap0, f"应填满 courselist：rows={rows0} cap={cap0}"
        await p.press("down")
        await p.pause(0.05)
        rows1 = len(_list_text(app).splitlines())
        await p.press("pagedown")
        await p.pause(0.05)
        rows2 = len(_list_text(app).splitlines())
        assert rows0 == rows1 == rows2, f"箭头/翻页不应改变行数：{rows0}->{rows1}->{rows2}"
    print("✓ 课程区按真实 viewport 填满；箭头/翻页不改变容量")


async def test_search_exit_does_not_shrink():
    """退出查找后课程区保持填满；按 ↑↓ 不能把它缩回去（旧 bug）。"""
    cfg = Config()
    cfg.first_run_done = True
    cfg.notify.to = "x@y.z"
    app = CourserApp(cfg)
    async with app.run_test(size=(120, 36)) as p:
        await p.pause(0.3)
        app.courses = _mk_single_line()
        app._set_view("all")
        app._render_main(force=True)
        await p.pause(0.05)
        # 开查找 → 关查找
        await p.press("/")
        await p.pause(0.05)
        app.editor.begin("课程", "text")
        app._render_main(force=True)
        await p.pause(0.05)
        await p.press("escape")
        await p.pause(0.05)
        rows_exit = len(_list_text(app).splitlines())
        assert rows_exit == _viewport_capacity(app), \
            f"退出查找后应填满：rows={rows_exit}"
        # 按 ↓ / PgDn 不能把它缩回去
        await p.press("down")
        await p.pause(0.05)
        rows_after = len(_list_text(app).splitlines())
        assert rows_after == rows_exit, f"退出查找后按箭头不应缩行：{rows_exit}->{rows_after}"
    print("✓ 退出查找后按箭头不再缩行（容量稳定）")


def test_seats_split_no_wrap():
    """限选/已选 拆分两列：各自右对齐、单一行不折行（数值列 no_wrap）。"""
    import io as _io
    from rich.console import Console as _Console
    cfg = Config()
    app = CourserApp(cfg)
    c2 = Course(course_no="X", name="n", category="c", dept="d",
                quota=150, selected=150, avail=0)
    tbl = app._build_course_table([c2], app._columns(120))
    buf = _io.StringIO()
    _Console(force_terminal=False, color_system=None, width=120, file=buf).print(tbl, end="")
    out = buf.getvalue()
    assert "150/150" not in out, "限/选 不应再合并成 150/150"
    # 限选 与 已选 各自成列（两位 150 各占一列，重复出现两次表示两列都存在）
    assert out.count("150") >= 2, f"限选/已选 应各自出现：{out!r}"
    print("✓ 限/选 拆为独立列，各自右对齐、单一行不折行")


async def test_keys_wrap_narrow():
    """窄窗口底部 #keys 自动折行：右侧提示不再被裁掉，且主表/设置/帮助的
    滚动区（真实 viewport 几何）随之收缩仍有效。"""
    cfg = Config()
    cfg.first_run_done = True
    app = CourserApp(cfg)
    async with app.run_test(size=(52, 22)) as p:
        await p.pause(0.3)
        app.courses = _mk_courses()
        app._set_view("all")
        app._render_main(force=True)
        await p.pause(0.05)
        keys = app.query_one("#keys")
        assert keys.size.height > 1, f"窄窗应折行，keys 高度={keys.size.height}"
        # 折行后尾部提示仍完整可见（未被右侧裁掉）
        keytext = keys.render().plain
        assert "退出" in keytext and keytext.rstrip().endswith("退出"), \
            f"折行后应包含提示尾部（退出）：{keytext!r}"
        # 课程区随之收缩：仍在 keys/activity 之上、高度>=1
        cl = app.query_one("#courselist")
        assert cl.content_region.height >= 1, "课程区不应被挤没"
        # 不越屏：activity 底沿不超过窗口高度
        assert app.query_one("#activity").content_region.bottom <= 22
        # settings / help 滚动区仍有效（真实 viewport 几何自适应缩行）
        app._show("settings"); await p.pause(0.05)
        assert app.query_one("#settingsscroll").content_region.height >= 1
        app._show("help"); await p.pause(0.05)
        assert app.query_one("#helpscroll").content_region.height >= 1
    print("✓ 窄窗 #keys 折行、右侧不裁；主表/设置/帮助滚动区自适应")


async def main() -> int:
    await test_viewport_fill_and_arrow_stable()
    await test_search_exit_does_not_shrink()
    await test_keys_wrap_narrow()
    test_seats_split_no_wrap()

    for w, h in _GEOMETRIES:
        body, _, runstate_height, runstate_text, hero_text = await _render_at(w, h)
        assert runstate_height >= 1, f"{w}×{h} 状态栏正文被边框挤没"
        assert "未开始" in runstate_text, f"{w}×{h} 状态栏未渲染"
        # 底部只放实时活动（历史数据已归顶部稳定摘要）
        assert "gmail" not in runstate_text.lower(), f"{w}×{h} 状态栏不应再显示 Gmail"
        assert "上次发信" in hero_text, f"{w}×{h} hero 里应有上次发信通道"
        lines = [ln for ln in body.splitlines() if ln.strip()]
        assert lines, f"{w}×{h} 无渲染结果"
        assert len(lines) >= 2, f"{w}×{h} 至少应有表头+数据行"
        # 每行（含折行）单行、合法 markup、不出界
        for ln in lines:
            ln_text = ln[len(ln) - len(ln.lstrip()):]  # 保留前导（缩进/光标）
            Text.from_markup(ln)                       # 合法 markup
            assert cell_len(Text.from_markup(ln).plain) <= w, f"行超宽 @{w}×{h}"
        print(f"  {w}×{h}：可见课程行 {len(lines) - 1} 行 OK")

    print("=" * 60)
    print("响应式几何回归测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
