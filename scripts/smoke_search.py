"""Headless smoke test for the main-page column search feature (live package app).

Run: .venv/bin/python scripts/smoke_search.py
（不写 data/、不连浏览器）
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from courser.config import Config  # noqa: E402
from courser.models import Course  # noqa: E402
from courser.tui.app import CourserApp  # noqa: E402

_tmpdir = tempfile.mkdtemp(prefix="courser-search-")
os.environ["COURSER_CONFIG"] = str(Path(_tmpdir) / "config.json")


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
        assert "[bold cyan]" in hl and hl.count("[bold cyan]") == 1, \
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

        # 4) 回车确认：退出输入态、查找词保留、过滤继续生效
        await pilot.press("enter")
        await pilot.pause()
        assert app.editing.context is None, "回车后应退出编辑态"
        assert app.main.search_query == "数学", app.main.search_query
        assert len(_visible(app)) == 0

        # 5) 再 / 修改：清空 → 回车提交 → 恢复 3 门；Esc 清除整条查找
        await pilot.press("/")
        await pilot.press("backspace", "backspace")
        await pilot.pause()
        assert app.editor.text == "", app.editor.text
        await pilot.press("enter")
        await pilot.pause()
        assert app.editing.context is None, "回车应退出编辑态"
        assert app.main.search_query == "", app.main.search_query
        assert len(_visible(app)) == 3, "清空查找词应恢复全部"
        # 查找仍激活（空词）→ Esc 清除整条查找
        await pilot.press("escape")
        await pilot.pause()
        assert app.main.search_col is None, "Esc 应清除整条查找"
        assert si.display is False, "清除后输入行应隐藏"

        # 6) 三个视图都提供 / 入口：1/2/3 下按 / 都能弹出查找框
        for view, key in (("all", "1"), ("matched", "2"), ("seats", "3")):
            await pilot.press(key)
            await pilot.pause()
            assert app.main.view == view, (view, app.main.view)
            await pilot.press("/")
            await pilot.pause()
            assert app.editing.context == "search", f"{view} 视图 / 应可用"
            await pilot.press("escape")   # 取消编辑
            await pilot.pause()
            await pilot.press("escape")   # 清除查找（若还开着）
            await pilot.pause()
            assert app.main.search_col is None, f"{view} 视图清除查找失败"
        # 回到全部视图
        await pilot.press("1")
        await pilot.pause()

        # 7) 空余视图 + 按「空余」列查找：先 tab 到 avail 再输数字
        await pilot.press("3")
        await pilot.pause()
        await pilot.press("/")
        await pilot.pause()
        assert app.main.search_col == "name"
        # name → cat → dept → teacher → seats → avail（5 次 tab）
        for _ in range(5):
            await pilot.press("tab")
            await pilot.pause()
        assert app.main.search_col == "avail", app.main.search_col
        await pilot.press("4")
        await pilot.pause()
        rows = _visible(app)
        assert len(rows) == 1 and rows[0].name == "中国哲学", rows
        await pilot.press("escape", "escape")
        await pilot.pause()
        assert len(_visible(app)) == 2, "seats 视图未查找时应只有有空余的 2 门"
        print("✓ 主页按列查找 headless 冒烟通过")

    print("=" * 60)
    print("search smoke test OK ✅")


if __name__ == "__main__":
    asyncio.run(main())