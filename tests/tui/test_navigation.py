"""TUI 导航回归测试（无头）。

覆盖：首启设置 → 主页视图 1/2/3 → 课程光标与详情 → 筛选 → 设置 →
日志/帮助返回 → 任意页 q / Ctrl+C 退出。
用法：uv run python tests/tui/test_navigation.py
（不启动监控、不连浏览器）
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_tmpdir = tempfile.mkdtemp(prefix="courser-tnav-")
os.environ["COURSER_CONFIG"] = str(Path(_tmpdir) / "config.json")

from textual.widgets import Button, Select, Switch  # noqa: E402

from courser.config import Config  # noqa: E402
from courser.models import Course  # noqa: E402
from courser.tui.app import CourserApp  # noqa: E402


def _assert_no_gui_widgets(app) -> None:
    """整页不允许出现任何图形控件（按钮/开关/下拉/表格）。"""
    for wtype in (Button, Select, Switch):
        assert not app.query(wtype), f"界面中不应出现 {wtype.__name__}"


async def test_main_hint_states():
    """主页底栏：(1) 无数据不提示列查找；(2) 搜索=持续 live filter（无编辑/浏览区分）
    ←→ 编辑关键词、[/] 跳页、Enter 详情、Esc 退出；宽窄屏各自压缩文案、
    按键能力一致。"""
    def states(app) -> tuple[str, str]:
        # 普通主页（有数据）
        app.main.search_col = None
        app.main.search_query = ""
        app.editing.context = None
        normal = app._main_hint()
        # 搜索中（live filter：编辑态常开，Enter 直接详情）
        app.editing.context = "search"
        app.main.search_col = "name"
        searching = app._main_hint()
        return normal, searching

    # 无数据：不提示「按列查找」（非挂载 app，宽度取 Textual 默认 → 窄屏）
    app0 = CourserApp(Config())
    app0.courses = []
    assert "按列查找" not in states(app0)[0] and "立即抓取" in states(app0)[0]

    _one = lambda: [Course(course_no="001", name="X", quota=1,
                           selected=0, avail=1)]

    # 宽屏：完整关键词
    wide = CourserApp(Config())
    wide.courses = _one()
    async with wide.run_test(size=(160, 40)):
        await asyncio.sleep(0.05)
        normal, searching = states(wide)
        assert "按列查找" in normal
        assert "编辑关键词" in searching and "查看详情" in searching
        assert "跳转页数" in searching and "退出查找" in searching

    # 窄屏：压缩文案，按键能力不变（不出现宽屏专属词）
    narrow = CourserApp(Config())
    narrow.courses = _one()
    async with narrow.run_test(size=(80, 24)):
        await asyncio.sleep(0.05)
        normal, searching = states(narrow)
        assert "按列查找" in normal
        assert "编辑" in searching and "详情" in searching
        assert "退出" in searching
        assert "移动输入光标" not in searching and "取消修改" not in searching
    print("✓ 主页提示：无数据隐藏按列查找；搜索=live filter 常开，宽窄屏各自压缩")


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
    app.main.snapshot_ts = "2026-09-07 12:00:00"
    app.main.snapshot_meta = "2 页 · 2 门课程"


async def _search_courses():
    """查找测试用课程：近独立 5 门 + 其它。"""
    base = [Course(course_no="001", name="近代物理实验", category="专业必修",
                   dept="物理学院", quota=30, selected=0, avail=1,
                   seq=f"s{i}", page=1) for i in range(5)]
    base.append(Course(course_no="002", name="英语写作", category="通识课(通选课I)",
                       dept="外国语学院", quota=50, selected=0, avail=1,
                       seq="s9", page=2))
    base.append(Course(course_no="003", name="高等数学", category="公共基础",
                       dept="数学科学学院", quota=80, selected=0, avail=1,
                       seq="s10", page=3))
    base.append(Course(course_no="004", name="数学分析", category="专业必修",
                       dept="数学科学学院", quota=40, selected=0, avail=1,
                       seq="s11", page=3))
    return base


async def test_search_live_filter():
    """/ 搜索 = 持续 live filter（删掉了编辑/浏览区分）。锁死核心规则：

    · 一旦 / 进入，只要不按 Esc 就保持编辑态（editor 常开，光标常在）；
    · ←→ 永远只是移动输入光标；↑↓/PgUp/PgDn 浏览结果；
    · [/] 在搜索中也跳课程页（复用 _move_page_cursor）；
    · Tab 换列、结果集变化回顶；
    · Enter 直接打开当前选中课程详情（不清编辑态，返回后可继续）；
    · Esc 在搜索列表退出整个搜索；在详情页则先返回搜索列表，再 Esc 才退出。
    """
    cfg = Config.load()
    cfg.first_run_done = True
    cfg.notify.to = "x@y.z"
    app = CourserApp(cfg)
    async with app.run_test(size=(120, 36)) as p:
        await p.pause(0.3)
        app.courses = await _search_courses()
        app._render_main(force=True)
        assert [k for k, _ in app._search_cols()] == ["no", "name", "cat", "dept"]

        # ---- NORMAL → SEARCH：editor 常开，caret 末尾 ----------------
        await p.press("/")
        await p.pause(0.05)
        assert app.editing.context == "search" and app.main.search_col == "name"
        assert app.editor.active and app.editor.text == "" and app.editor.caret == 0

        # 输入即筛：canonical(query) 同步 → "数学" 收窄到 2 门
        app.editor.begin("数学", "text")
        app.main.search_query = app.editor.text   # 真实输入经 _edit_key 同步；测试直接设等价态
        app._render_main(force=True)
        await p.pause(0.05)
        assert app.main.search_query == "数学"
        rows = app._visible_rows()
        assert len(rows) == 2, f"输入即筛应收窄到 2，实得 {len(rows)}"

        # ← 移动输入光标（不是跳页）；↑↓ 浏览结果
        await p.press("left")
        await p.pause(0.05)
        assert app.editor.caret == 1, "← 应左移输入光标"
        assert app.main.index == 0, "移动输入光标不应改变结果光标"
        await p.press("down")
        await p.pause(0.05)
        assert app.main.index == 1, "↓ 应浏览结果"
        assert app.editor.caret == 1, "浏览结果不应动输入光标"

        # Tab 换列：结果集变化 → 回顶
        await p.press("tab")
        await p.pause(0.05)
        assert app.main.search_col == "cat", "Tab 应切到下一搜索列"
        assert app.editing.context == "search", "Tab 不退出搜索"
        assert app.main.index == 0, "换列导致结果集变化应回顶"
        # 切回课程名列，便于后续断言
        app.main.search_col = "name"
        app._render_main(force=True)
        await p.pause(0.05)

        # ---- Esc 彻底退出搜索 ----------------------------------------
        await p.press("escape")
        await p.pause(0.05)
        assert app.main.search_col is None and app.main.search_query == ""
        assert app.editing.context is None

        # ---- 空查词 + [/] 跳课程页（搜索中编辑态也跳页，不退出）--------
        await p.press("/")
        await p.pause(0.05)
        assert app.editor.active
        rows_all = app._visible_rows()
        assert len(rows_all) == 8, f"空查词应列出全部，实得 {len(rows_all)}"
        await p.press("]")
        await p.pause(0.05)
        assert app.main.index == next(i for i, c in enumerate(rows_all) if c.page == 2), \
            "搜索中 ] 应跳到下一页第一门课"
        assert app.editor.active, "跳页不应退出搜索/编辑"
        await p.press("[")
        await p.pause(0.05)
        assert app.main.index == next(i for i, c in enumerate(rows_all) if c.page == 1), \
            "搜索中 [ 应回到上一页"

        # ---- Enter 打开详情；详情页 Esc 返回搜索，再 Esc 退出 ----------
        assert app.editor.active, "此时仍在搜索态"
        await p.press("enter")
        await p.pause(0.1)
        assert app.page == "detail", "搜索中 Enter 应打开选中课程详情"
        assert app.editing.context == "search", "进详情不清搜索编辑态（返回可续）"
        await p.press("escape")
        await p.pause(0.1)
        assert app.page == "main", "详情页 Esc 应返回搜索列表"
        assert app.editing.context == "search" and app.main.search_col == "name", \
            "返回后应还在搜索态"
        await p.press("escape")
        await p.pause(0.05)
        assert app.main.search_col is None and app.editing.context is None, \
            "搜索中 Esc 应彻底退出搜索"
    print("✓ / 搜索=live filter：Enter 直接详情、Esc 退出；[/] 搜索也跳页；←→ 只移光标")


def test_view_hierarchy() -> None:
    """1 全部 ⊇ 2 符合筛选 ⊇ 3 有空余；空筛选时 2/3 自然退化。"""
    cfg = Config()
    app = CourserApp(cfg)
    app.courses = [
        Course(course_no="001", name="目标课", quota=10, selected=9, avail=1),
        Course(course_no="002", name="其它课", quota=10, selected=9, avail=1),
        Course(course_no="003", name="目标满额", quota=10, selected=10, avail=0),
    ]

    app.main.view = "matched"
    assert len(app._visible_rows()) == 3, "空筛选时视图 2 应退化为全部"
    app.main.view = "seats"
    assert [c.name for c in app._visible_rows()] == ["目标课", "其它课"], \
        "空筛选时视图 3 应显示所有有空余课程"

    cfg.filters.names = ["目标"]
    app.main.view = "matched"
    assert [c.name for c in app._visible_rows()] == ["目标课", "目标满额"]
    app.main.view = "seats"
    assert [c.name for c in app._visible_rows()] == ["目标课"], \
        "视图 3 必须继承筛选并再要求有空余"
    print("✓ 视图层级：全部 ⊇ 符合筛选 ⊇ 有空余")


async def main() -> int:
    await test_main_hint_states()
    await test_search_live_filter()
    test_view_hierarchy()
    # ---- 首启设置：填邮箱 → 就绪 → 主页 ----
    cfg = Config.load()
    cfg.first_run_done = False
    app = CourserApp(cfg)
    async with app.run_test(size=(120, 36)) as p:
        await p.pause(0.3)
        assert app.page == "setup", f"首次启动应在设置向导页，实际 {app.page}"
        _assert_no_gui_widgets(app)
        await p.press("down")
        await p.pause(0.1)
        assert app.editing.context == "setup" and app.editor.active, \
            "↓ 应进入收件邮箱行内编辑"
        app.editor.begin("me@example.com", "text")
        app._render_setup()
        await p.press("enter")
        await p.pause(0.1)
        assert app.cfg.notify.to == "me@example.com", "收件邮箱应写入"
        await p.press("enter")
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
        await p.press("3")
        await p.pause(0.1)
        assert app.main.view == "seats" and len(app._visible_rows()) == 1, "视图3应只剩空余"
        await p.press("2")
        await p.pause(0.1)
        assert app.main.view == "matched" and len(app._visible_rows()) == 1, "视图2应只看符合筛选"
        await p.press("1")
        await p.pause(0.1)
        assert app.main.view == "all" and len(app._visible_rows()) == 2
        await p.press("down")
        await p.pause(0.05)
        assert app.main.index == 1
        await p.press("enter")
        await p.pause(0.1)
        assert app.page == "detail"
        body = str(app.query_one("#detbody").render())
        assert "普通物理" in body and "课程号" in body, "详情应含完整课程信息"
        await p.press("escape")
        await p.pause(0.1)
        assert app.page == "main"

        # 筛选：搜索框与候选列表是一条纵向焦点链，Space 语义由焦点决定。
        await p.press("f")
        await p.pause(0.2)
        assert app.page == "filters"
        assert app.fv.dim == 0 and app.fv.query == "" and app.fv.focus == "search"
        await p.press("a")
        await p.pause(0.1)
        assert app.fv.query == "a", "输入应进入顶部搜索行"
        await p.press("space")
        await p.pause(0.1)
        assert app.fv.query == "a ", "搜索框焦点下 Space 应输入空格"
        await p.press("backspace")
        await p.pause(0.1)
        assert app.fv.query == "a"
        await p.press("backspace")
        await p.pause(0.1)
        assert app.fv.query == ""
        app._filters_type("英语")
        assert app.fv.query == "英语" and app._filters_items() == ["英语写作"], \
            "输入即筛应收窄列表"
        app.fv.query = ""
        app._render_filters_list()
        await p.press("down")
        await p.pause(0.1)
        assert app.fv.focus == "list" and app.fv.index == 0, "↓ 应从搜索进入首个候选"
        await p.press("up")
        await p.pause(0.1)
        assert app.fv.focus == "search", "列表首项 ↑ 应回到搜索框"
        await p.press("tab")
        await p.pause(0.1)
        assert app.fv.dim == 1 and app.fv.focus == "search", "Tab 应切维度并回到搜索"
        await p.press("down")
        await p.pause(0.1)
        await p.press("space")
        await p.pause(0.1)
        assert app.cfg.filters.categories == [], "空格应取消默认类别"
        await p.press("space")
        await p.pause(0.1)
        assert app.cfg.filters.categories == ["英语类"], "再按空格应重新选中"
        await p.press("enter")
        await p.pause(0.2)
        assert app.page == "main", "筛选回车应保存并返回"
        # 再进，改动后 Esc 放弃
        await p.press("f")
        await p.pause(0.2)
        await p.press("tab")
        await p.pause(0.1)
        await p.press("down")
        await p.pause(0.1)
        await p.press("space")
        await p.pause(0.1)
        assert app.cfg.filters.categories == [], "进入后空格应移除"
        await p.press("escape")
        await p.pause(0.2)
        assert app.page == "main", "Esc 应放弃并返回"
        assert app.cfg.filters.categories == ["英语类"], "Esc 应还原修改"

        # 筛选：无匹配输入 + 回车 = 自定义条目
        await p.press("f")
        await p.pause(0.2)
        app.fv.query = "物理学院课程"
        app._render_filters_list()
        await p.press("enter")
        await p.pause(0.2)
        assert "物理学院课程" in app.cfg.filters.names, "无匹配回车应加入自定义条目"
        assert app.fv.query == "", "加入后应清空搜索"
        await p.press("down")
        await p.pause(0.1)
        await p.press("enter")
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

        # 设置：编辑收件邮箱 → ctrl+s 保存 → 主页；再 Esc 放弃
        await p.press("s")
        await p.pause(0.2)
        assert app.page == "settings"
        # 设置页必须有固定 #keys 操作栏（正文滚动也不会把底部 footer 挤掉）
        assert app.query_one("#keys").display, "设置页应显示固定 #keys 操作栏"
        assert "↑↓" in str(app.query_one("#keys").render()), "设置页应显示页面级操作提示"
        for _ in range(2):
            await p.press("down")
        await p.pause(0.05)
        _g, f = app.s_rows[app.sv.index]
        assert f["key"] == "to", f"光标应到收件邮箱，实际 {f['key']}"
        await p.press("enter")
        await p.pause(0.1)
        assert app.editing.context == "settings" and app.editing.key == "to" \
            and app.editor.active, "回车应进入该行的行内编辑态"
        # 字段编辑态下 #keys 应切到编辑键位提示
        assert "移动光标" in str(app.query_one("#keys").render()), \
            "编辑态底部应切到编辑键位提示"
        app.editor.begin("ab")
        app._render_settings_list()
        await p.press("left")
        await p.pause(0.05)
        assert app.editor.caret == 1, "← 应左移光标"
        await p.press("backspace")
        await p.pause(0.05)
        assert app.editor.text == "b" and app.editor.caret == 0, \
            "退格应删除光标前字符（原位，不清空重输）"
        app.editor.begin("x@y.com")
        app._render_settings_list()
        await p.press("enter")
        await p.pause(0.1)
        assert app.editing.context is None and app.sd["to"] == "x@y.com", "回车应确认编辑"
        await p.press("ctrl+s")
        await p.pause(0.2)
        assert app.page == "main" and app.cfg.notify.to == "x@y.com", "ctrl+s 应保存并返回"
        await p.press("s")
        await p.pause(0.2)
        for _ in range(2):
            await p.press("down")
        await p.press("enter")
        await p.pause(0.05)
        app.editor.begin("zz@zz")
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
        qa.fv.auto_gather = False   # 测试不触发真实抓取
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

    print("TUI 导航 OK: setup/主页视图/详情/筛选/设置/日志/帮助 + q/Ctrl+C 均正常")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
