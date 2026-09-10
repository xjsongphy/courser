"""Headless smoke test for the main-page column search feature (live package app).

Run: .venv/bin/python scripts/smoke_search.py
（不写 data/、不连浏览器）
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必须在导入 courser.config 之前设置：CONFIG_PATH 在模块 import 时即定值，
# 若此时才设，会把真实 config.json 当配置源，筛选条件会影响 seats 视图（非唯一）。
_tmpdir = tempfile.mkdtemp(prefix="courser-search-")
os.environ["COURSER_CONFIG"] = str(Path(_tmpdir) / "config.json")

from courser.config import Config  # noqa: E402
from courser.models import Course  # noqa: E402
from courser.tui.app import CourserApp  # noqa: E402


def _fake(app) -> None:
    """注入课程快照（只改内存，不写 data/）。"""
    app.courses = [
        Course(course_no="04830120", name="高等数学(B)(一)", category="专业必修",
               dept="数学科学学院", teacher="张三", seats_raw="150 / 149",
               quota=150, selected=149, avail=1, status="可申请", page=2, seq="s1"),
        Course(course_no="02030310", name="普通物理", category="全校必修",
               dept="物理学院", teacher="李四", seats_raw="60 / 60",
               quota=60, selected=60, avail=0, status="可申请", page=3, seq="s2"),
        Course(course_no="01630040", name="中国哲学", category="任选、思政选择性必修",
               dept="哲学系", teacher="王五", seats_raw="80 / 76",
               quota=80, selected=76, avail=4, status="可申请", page=1, seq="s3"),
    ]
    app.candidate_lists = {
        "names": [c.name for c in app.courses],
        "categories": ["专业必修", "全校必修", "任选", "思政选择性必修"],
        "depts": ["数学科学学院", "物理学院", "哲学系"],
    }
    app.main.snapshot_ts = "2025-01-01 00:00:00"
    app.main.snapshot_meta = "3 页 · 3 门课程"


def _visible(app):
    """当前可见行（应用内部语义，不受渲染折行影响）。"""
    return app._visible_rows()


async def main():
    cfg = Config.load()
    cfg.first_run_done = True          # 测试直接进入主页
    app = CourserApp(cfg)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        assert app.page == "main", f"应进入主页，实际 {app.page}"
        _fake(app)
        app._render_main(force=True)
        await pilot.pause()
        si = app.query_one("#searchinput")
        assert si.display is False, "未启用查找时 input 行应隐藏"
        assert len(_visible(app)) == 3, f"初始应 3 门课，实际 {len(_visible(app))}"

        # 1) / 启动查找：默认课程列，弹出输入框
        await pilot.press("/")
        await pilot.pause()
        assert app.main.search_col == "name", app.main.search_col
        assert app.editing.context == "search", "应处于查找编辑态"
        assert si.display is True, "启动查找后输入框应显示"
        # 表头里被查找列的列名应高亮：_header_labels 对该列产出 [bold cyan]…[/]
        cols = app._columns(max(40, app.size.width - 4))
        hl = app._header_labels(cols)
        assert "[bold #58A6FF]" in hl and hl.count("[bold #58A6FF]") == 1, \
            "查找列列名应恰有一个高亮"

        # 2) 输入即筛：输「数学」→ 只剩 高等数学
        await pilot.press("数", "学")
        await pilot.pause()
        assert app.editor.text == "数学", app.editor.text
        rows = _visible(app)
        assert len(rows) == 1 and "高等数学" in rows[0].name, rows

        # 3) Tab 换列：name → cat，类别列按「数学」无结果
        await pilot.press("tab")
        await pilot.pause()
        assert app.main.search_col == "cat", app.main.search_col
        assert len(_visible(app)) == 0, "类别列按「数学」应无结果"

        # 4) 查找编辑态常开：Enter=打开详情（当前 cat 列空词→0 行，无行不应跳详情/崩溃）
        #    Esc 才退出查找。这与 _main_hint「Enter 查看详情 / Esc 退出查找」一致。
        await pilot.press("enter")
        await pilot.pause()
        assert app.page == "main", "0 行时 Enter 不应打开详情"
        assert app.editing.context == "search", "Enter 不退出编辑态（编辑态常开）"
        assert app.main.search_query == "数学", app.main.search_query

        # 5) Esc 退出查找：编辑态常开靠 Esc 关闭，清空整条查找、恢复全部
        await pilot.press("escape")
        await pilot.pause()
        assert app.editing.context is None, "Esc 应退出查找编辑态"
        assert app.main.search_col is None, "Esc 应清除整条查找"
        assert si.display is False, "退出后输入行应隐藏"
        assert len(_visible(app)) == 3, "清除查找应恢复全部 3 门"

        # 6) 有结果时 Enter 打开当前行详情；返回后编辑态仍在（常开）；三视图都提供 / 入口
        await pilot.press("/")
        await pilot.press("数", "学")
        await pilot.pause()
        rows = _visible(app)
        assert len(rows) == 1 and "高等数学" in rows[0].name, rows
        await pilot.press("enter")
        await pilot.pause()
        assert app.page == "detail", "Enter 应打开当前行详情"
        assert app.editing.context == "search", "详情页编辑态仍常开"
        await pilot.press("escape")   # 回到主页；编辑态仍在
        await pilot.pause()
        assert app.page == "main"
        assert app.editing.context == "search"
        await pilot.press("escape")   # 退出查找
        await pilot.pause()
        assert app.editing.context is None
        assert len(_visible(app)) == 3, "退出查找应恢复全部 3 门"

        for view, key in (("all", "1"), ("matched", "2"), ("seats", "3")):
            await pilot.press(key)
            await pilot.pause()
            assert app.main.view == view, (view, app.main.view)
            await pilot.press("/")
            await pilot.pause()
            assert app.editing.context == "search", f"{view} 视图 / 应可用"
            await pilot.press("escape")   # 编辑态常开 → 一次 Esc 即可清除整条查找
            await pilot.pause()
            assert app.main.search_col is None, f"{view} 视图清除查找失败"
        # 回到全部视图
        await pilot.press("1")
        await pilot.pause()

        # 7) 空余视图 + 按「开课院系」列查找：tab 到 dept 再输入
        await pilot.press("3")
        await pilot.pause(0.1)
        await pilot.press("/")
        await pilot.pause(0.1)
        assert app.main.search_col == "name"
        for _ in range(4):
            if app.main.search_col == "dept":
                break
            await pilot.press("tab")
            await pilot.pause(0.05)
        assert app.main.search_col == "dept", app.main.search_col
        await pilot.press("哲")
        await pilot.pause()
        rows = _visible(app)
        assert len(rows) == 1 and rows[0].name == "中国哲学", [r.name for r in rows]
        await pilot.press("escape")
        await pilot.pause()
        assert app.main.search_col is None, "Esc 应清除查找"
        assert len(_visible(app)) == 2, "seats 视图未查找时应只有有空余的 2 门"
        print("✓ 主页按列查找 headless 冒烟通过")

    print("=" * 60)
    print("search smoke test OK ✅")


if __name__ == "__main__":
    asyncio.run(main())