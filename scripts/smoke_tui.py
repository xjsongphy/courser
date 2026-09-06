"""TUI 无头冒烟测试：用 textual run_test 驱动界面，验证各界面能正常打开/交互。

用法：
    uv run python scripts/smoke_tui.py
（不启动监控、不连浏览器）
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 使用临时配置，避免冒烟测试污染真实 config.json
_tmpdir = tempfile.mkdtemp(prefix="courser-smoke-")
os.environ["COURSER_CONFIG"] = str(Path(_tmpdir) / "config.json")

from courser.config import Config  # noqa: E402
from courser.tui import (CourserApp, FilterScreen, FirstRunScreen,  # noqa: E402
                         HelpScreen, IntervalModal, SettingsScreen)


async def main() -> int:
    cfg = Config.load()
    cfg.first_run_done = False  # 验证首次向导会弹出
    app = CourserApp(cfg)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        assert isinstance(app.screen, FirstRunScreen), \
            f"expect FirstRunScreen on first launch, got {type(app.screen)}"
        app.pop_screen()  # 关掉向导，进入主界面
        await pilot.pause(0.1)
        # 帮助
        app.action_open_help()
        await pilot.pause()
        assert isinstance(app.screen, HelpScreen), f"expect HelpScreen, got {type(app.screen)}"
        app.pop_screen()
        await pilot.pause()

        # 筛选：pi 风格添加/切换维度/切换命中模式
        app.action_open_filters()
        await pilot.pause()
        fs = app.screen
        assert isinstance(fs, FilterScreen)
        fs.query_one("#query").value = "通识核心课I类"
        fs._on_query_submit(None)  # 回车：自定义添加
        fs.action_group("2")
        fs.action_toggle_match()
        await pilot.pause()
        assert "通识核心课I类" in fs.entries["names"], f"entries={fs.entries['names']}"
        assert app.cfg.filters.match == "all"
        fs.action_close()
        await pilot.pause()

        # 设置（唯一入口）
        app.action_open_settings()
        await pilot.pause()
        s = app.screen
        assert isinstance(s, SettingsScreen)
        s.query_one("#set_interval").value = "10"
        s._apply()
        assert app.cfg.interval_min == 10.0
        app.pop_screen()
        await pilot.pause()

        # 视图切换 + 间隔弹窗
        app.action_toggle_view()
        app.action_set_interval_dialog()
        await pilot.pause()
        assert isinstance(app.screen, IntervalModal)
        app.pop_screen()
        await pilot.pause()

        # 表格渲染（空数据 + 假数据各一次）
        app.render_table()
        from courser.fetch import Course
        app.courses = [Course(course_no="001", name="测试课", category="通识课(通识核心课I类)",
                              dept="英语语言文学系", quota=50, selected=49, avail=1,
                              seq="X1")]
        app.render_table()
        await pilot.pause()

    print("TUI smoke OK: help/filter/settings/view/interval/table 均正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))