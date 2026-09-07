"""TUI 无头冒烟测试：驱动重构后的纯文本监控 TUI。

覆盖：首启设置（填邮箱→就绪→进入主页）→ 主页视图 1/2/3 → 课程光标与详情 →
筛选（Tab 维度 / 空格切换 / 回车保存 / Esc 放弃）→ 设置（draft 保存/放弃）→
日志 / 帮助返回 → 任意页 q / Ctrl+C 退出。

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

from textual.widgets import Button, Input, Select, Switch  # noqa: E402

from courser.config import Config  # noqa: E402
from courser.fetch import Course  # noqa: E402
from courser.tui import CourserApp  # noqa: E402


def _assert_no_gui_widgets(app) -> None:
    """整页不允许出现任何图形控件（按钮/开关/下拉/表格）。"""
    for wtype in (Button, Select, Switch):
        assert not app.query(wtype), f"界面中不应出现 {wtype.__name__}"


def _fake(app) -> None:
    app.courses = [
        Course(course_no="001", name="英语写作", category="英语类",
               dept="英语系", teacher="张老师", quota=50, selected=49, avail=1,
               seats_raw="50/49", status="可申请", seq="a", page=1),
        Course(course_no="002", name="普通物理", category="物理类",
               dept="物理学院", teacher="李老师", quota=60, selected=60, avail=0,
               seats_raw="60/60", status="不可申请", seq="b", page=2),
    ]
    app.candidate_lists = {"names": ["英语写作", "普通物理"],
                           "categories": ["英语类", "物理类"],
                           "depts": ["英语系", "物理学院"]}
    app.snapshot_ts = "2026-09-07 12:00:00"
    app.snapshot_meta = "2 页 · 2 门课程"


async def main() -> int:
    # ---- 首启设置：填邮箱 → 就绪 → 主页 ----
    cfg = Config.load()
    cfg.first_run_done = False
    app = CourserApp(cfg)
    async with app.run_test(size=(120, 36)) as p:
        await p.pause(0.3)
        assert app.page == "setup", f"首次启动应在设置向导页，实际 {app.page}"
        _assert_no_gui_widgets(app)
        # 未配置时按 1 不会开监控（此路径不可达，因为主页未进入）；跳过
        # 用 ↓ 选中并编辑收件邮箱
        await p.press("down")
        await p.pause(0.1)
        si = app.query_one("#setup_input", Input)
        assert si.has_focus and app._editing == "setup", "首启应出现底部单行输入"
        si.value = "me@example.com"
        await p.press("enter")
        await p.pause(0.1)
        assert app.cfg.notify.to == "me@example.com", "收件邮箱应写入"
        await p.press("enter")  # 就绪后回车开始使用
        await p.pause(0.2)
        assert app.page == "main", f"就绪回车应进入主页，实际 {app.page}"
        assert app.cfg.first_run_done, "完成首启应置 first_run_done"
        _assert_no_gui_widgets(app)
        await p.press("q")

    # ---- 主页：视图 / 光标 / 详情 / 各页返回 ----
    cfg2 = Config.load()
    cfg2.first_run_done = True
    cfg2.filters.categories = ["英语类"]
    app = CourserApp(cfg2)
    async with app.run_test(size=(120, 36)) as p:
        await p.pause(0.3)
        assert app.page == "main"
        _fake(app)
        app._render_main(force=True)
        # 视图 3（只看空余）→ 只剩空余课程；视图 1 全部
        await p.press("3")
        await p.pause(0.1)
        assert app.view == "seats" and len(app._visible_rows()) == 1, "视图3应只剩空余"
        await p.press("2")
        await p.pause(0.1)
        assert app.view == "matched" and len(app._visible_rows()) == 1, "视图2应只看符合筛选"
        await p.press("1")
        await p.pause(0.1)
        assert app.view == "all" and len(app._visible_rows()) == 2
        # 光标下移 + 详情
        await p.press("down")
        await p.pause(0.05)
        assert app.c_idx == 1
        await p.press("enter")
        await p.pause(0.1)
        assert app.page == "detail"
        body = str(app.query_one("#detbody").render())
        assert "普通物理" in body and "课程号" in body, "详情应含完整课程信息"
        await p.press("escape")
        await p.pause(0.1)
        assert app.page == "main"

        # 筛选（pi 式）：输入即筛 / Tab 维度 / 空格切换 / 回车保存
        await p.press("f")
        await p.pause(0.2)
        assert app.page == "filters"
        assert app.f_dim == 0 and app.f_query == ""
        await p.press("a")          # 键入字符进入顶部搜索行
        await p.pause(0.1)
        assert app.f_query == "a", "输入应进入顶部搜索行"
        await p.press("backspace")
        await p.pause(0.1)
        assert app.f_query == ""
        app._filters_type("英语")
        assert app.f_query == "英语" and app._filters_items() == ["英语写作"], \
            "输入即筛应收窄列表"
        app.f_query = ""
        app._render_filters_list()
        await p.press("tab")
        await p.pause(0.1)
        assert app.f_dim == 1, "Tab 应切到课程类别"
        await p.press("space")
        await p.pause(0.1)
        assert app.cfg.filters.categories == [], "空格应取消默认类别"
        await p.press("space")
        await p.pause(0.1)
        assert app.cfg.filters.categories == ["英语类"], "再按空格应重新选中"
        await p.press("enter")
        await p.pause(0.2)
        assert app.page == "main", "筛选回车应保存并返回"
        # 再次进入，改动后 Esc 放弃
        await p.press("f")
        await p.pause(0.2)
        await p.press("tab")   # 课程类别
        await p.pause(0.1)
        await p.press("space")
        await p.pause(0.1)
        assert app.cfg.filters.categories == [], "进入后空格应移除"
        await p.press("escape")
        await p.pause(0.2)
        assert app.page == "main", "Esc 应放弃并返回"
        assert app.cfg.filters.categories == ["英语类"], "Esc 应还原修改"

        # 筛选：无匹配输入 + 回车 = 作为自定义条目加入当前维度
        await p.press("f")
        await p.pause(0.2)
        app.f_query = "物理学院课程"
        app._render_filters_list()
        await p.press("enter")
        await p.pause(0.2)
        assert "物理学院课程" in app.cfg.filters.names, "无匹配回车应加入自定义条目"
        assert app.f_query == "", "加入后应清空搜索"
        await p.press("enter")   # 空输入回车：保存并返回
        await p.pause(0.2)
        assert app.page == "main"
        assert "物理学院课程" in app.cfg.filters.names, "自定义条目应已保存"

        # 日志 / 帮助
        await p.press("l")
        await p.pause(0.1)
        assert app.page == "logs"
        await p.press("escape")
        await p.pause(0.1)
        assert app.page == "main"
        await p.press("h")
        await p.pause(0.1)
        assert app.page == "help"
        await p.press("escape")
        await p.pause(0.1)
        assert app.page == "main"

        # 设置：编辑收件邮箱 → ctrl+s 保存 → 主页
        await p.press("s")
        await p.pause(0.2)
        assert app.page == "settings"
        for _ in range(2):
            await p.press("down")
        await p.pause(0.05)
        _g, f = app.s_rows[app.s_idx]
        assert f["key"] == "to", f"光标应到收件邮箱，实际 {f['key']}"
        await p.press("enter")
        await p.pause(0.1)
        assert app._editing == "settings" and app._editing_key == "to", \
            "回车应进入该行的行内编辑态"
        # 原位编辑：保留值、←/→移动光标、退格删除光标前的字符
        app._edit_text = "ab"
        app._edit_caret = len("ab")
        app._render_settings_list()
        await p.press("left")
        await p.pause(0.05)
        assert app._edit_caret == 1, "← 应左移光标"
        await p.press("backspace")
        await p.pause(0.05)
        assert app._edit_text == "b" and app._edit_caret == 0, \
            "退格应删除光标前字符（原位，不清空重输）"
        app._edit_text = "x@y.com"
        app._edit_caret = len("x@y.com")
        app._render_settings_list()
        await p.press("enter")      # 回车确认本行编辑
        await p.pause(0.1)
        assert app._editing is None and app.sd["to"] == "x@y.com", "回车应确认编辑"
        await p.press("ctrl+s")
        await p.pause(0.2)
        assert app.page == "main" and app.cfg.notify.to == "x@y.com", "ctrl+s 应保存并返回"
        # 设置放弃：行内编辑确认后，Esc 离开不保存 → 还原
        await p.press("s")
        await p.pause(0.2)
        for _ in range(2):
            await p.press("down")
        await p.press("enter")
        await p.pause(0.05)
        app._edit_text = "zz@zz"
        app._edit_caret = len("zz@zz")
        app._render_settings_list()
        await p.press("enter")
        await p.pause(0.05)
        assert app.sd["to"] == "zz@zz"
        await p.press("escape")
        await p.pause(0.2)
        assert app.page == "main" and app.cfg.notify.to == "x@y.com", "Esc 应放弃不保存"
        await p.press("q")

    # ---- q / Ctrl+C：任意页退出 ----
    async def _quit_case(page_key: str | None, key: str) -> None:
        qc = Config.load()
        qc.first_run_done = True
        qa = CourserApp(qc)
        qa.auto_gather = False   # 测试不触发真实抓取
        async with qa.run_test(size=(100, 30)) as p2:
            await p2.pause(0.3)
            if page_key:
                await p2.press(page_key)
                await p2.pause(0.2)
            await p2.press(key)
            await p2.pause(0.3)
            assert getattr(qa, "_exit", False) or not qa.is_running, \
                f"{key} 未退出：page={qa.page}"

    for page_key in (None, "s", "l", "h"):
        await _quit_case(page_key, "q")
    for page_key in (None, "f", "s"):
        await _quit_case(page_key, "ctrl+c")

    print("TUI smoke OK: setup/主页视图/详情/筛选/设置/日志/帮助 + q/Ctrl+C 均正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
