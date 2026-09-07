"""表格布局不变量测试（对齐到 courser.tui.app 现在的 `_course_lines` API）。

覆盖当前"单格可折行、整行网格对齐"设计下我们真正关心的不变量：
- 每条物理行不超出终端宽度（整行网格不出界）
- 任一列的内容不透出到相邻列（列宽被严格限制，不会"跑到下一行"）
- 表头与数据行的列起始一致（列对齐）
- 光标(❯)/匹配(★)只占各自格位，不移动其它列
- 必保列 (页/课程号/课程/限选-已选/空余) 总是存在，课程名列有正宽度
- 超长课程名折行后内容完整保留（不截断、不加省略号）

用法：uv run python tests/tui/test_table_layout.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from rich.cells import cell_len
from rich.text import Text

from courser.config import Config
from courser.models import Course
from courser.tui.app import CourserApp

_LONG = "计算机科学与技术导论与程序设计基础（含实验）"
_COURSES = [
    Course(course_no="001", name=_LONG, category="通识课(通识核心课III)",
           dept="信息科学技术学院", teacher="张伟(教授)", quota=100, selected=99,
           avail=1, seats_raw="100/99", seq="a", page=1),
    Course(course_no="002", name="英语写作", category="英语类",
           dept="英语语言文学系", teacher="王芳(副教授)", quota=50, selected=49,
           avail=1, seats_raw="50/49", seq="b", page=2),
    Course(course_no="003", name="普通物理", category="物理类",
           dept="物理学院", teacher="李强(教授)", quota=60, selected=60,
           avail=0, seats_raw="60/60", seq="c", page=3),
]


def _plain(s: str) -> str:
    return Text.from_markup(s).plain


def _extract(plain: str, start: int, width: int) -> str:
    """取落在 [start, start+width) 显示格里的字符（中文按 2 格）。"""
    acc, out = 0, []
    for ch in plain:
        cw = cell_len(ch)
        if start <= acc < start + width:
            out.append(ch)
        acc += cw
        if acc >= start + width:
            break
    return "".join(out)


def _col_starts(widths, lead: int = 3) -> list[int]:
    """按『光标槽 + 每列宽 + 列间 1 空格』推算各列起始列（格单位）。"""
    pos, out = lead, []
    for w in widths:
        out.append(pos)
        pos += w + 1
    return out


def _assert_grid_integrity(lines, widths, W) -> None:
    """单条课程的全部物理行：不出界、任一列不透出到邻列。"""
    starts = _col_starts(widths)
    for line in lines:
        plain = _plain(line)
        assert cell_len(plain) <= W, f"物理行宽度 {cell_len(plain)} 超出 {W}"
        for (start, w) in zip(starts, widths):
            seg = _extract(plain, start, w)
            # 列内内容（去填充）不得超过列宽 → 不透出邻列、不悔到下一行
            assert cell_len(seg.rstrip()) <= w, f"列内容 {seg!r} 溢出列宽 {w}"


def _make_app() -> CourserApp:
    app = CourserApp(Config())
    app.main.search_col = None
    return app


def test_header_data_aligned_and_grid_contained():
    app = _make_app()
    for W in (60, 80, 100, 140):
        cols = app._columns(W)
        widths = [w for _k, _l, w in cols]
        hdr = _plain(app._header_labels(cols))
        h_starts = _col_starts(widths)
        # 表头每个列标签落在对应列内
        for (start, (_k, label, w)) in zip(h_starts, cols):
            assert label in _extract(hdr, start, w), f"表头 {label} 列位错"
        for c in _COURSES:
            for matched in (False, True):
                lines = app._course_lines(c, matched, cols, cursor=True)
                assert lines, "至少渲染出一行"
                for line in lines:
                    assert _col_starts(widths) == h_starts, "行与表头列起始不一致"
                _assert_grid_integrity(lines, widths, W)
    print("✓ 表头/数据列对齐；整行网格不出界、列不透出")


def test_no_column_spills_past_width():
    app = _make_app()
    for W in (60, 80, 100, 120, 160):
        cols = app._columns(W)
        widths = [w for _k, _l, w in cols]
        assert app._table_width(cols) <= W, f"表格总宽 {app._table_width(cols)} > {W}"
        for c in _COURSES:
            lines = app._course_lines(c, False, cols, False)
            lines += app._course_lines(c, True, cols, True)
            _assert_grid_integrity(lines, widths, W)
    print("✓ 任何列内容都不超过其列宽（不跑到邻列/下一行）")


def test_cursor_confined_to_gutter():
    app = _make_app()
    W = 90
    cols = app._columns(W)
    widths = [w for _k, _l, w in cols]
    c = _COURSES[0]
    cur = app._course_lines(c, False, cols, cursor=True)
    non = app._course_lines(c, False, cols, cursor=False)
    assert _plain(cur[0]).startswith("❯  "), "光标行首应有固定 3 格光标槽"
    assert not _plain(non[0]).startswith("❯"), "非光标行行首不应有 ❯"
    _assert_grid_integrity(cur, widths, W)
    print("✓ 光标(❯)只占 3 格光标槽，不移动列")


def test_matched_star_confined_to_name_cell():
    app = _make_app()
    W = 90
    cols = app._columns(W)
    widths = [w for _k, _l, w in cols]
    starts = _col_starts(widths)
    name_i = [i for i, (k, _l, _w) in enumerate(cols) if k == "name"][0]
    name_w = widths[name_i]
    c = _COURSES[0]
    plain_row = app._course_lines(c, False, cols, False)
    matched_row = app._course_lines(c, True, cols, False)
    # 两版各物理行网格完全一致
    for pa, mb in zip(plain_row, matched_row):
        assert _col_starts(widths) == _col_starts(
            [w for _k, _l, w in cols])
        _assert_grid_integrity([pa, mb], widths, W)
    # ★ 落在课程名列内第一行最左侧
    first = _extract(_plain(matched_row[0]), starts[name_i], name_w)
    assert "★" in first, "★ 应落在课程名列内"
    print("✓ 匹配 ★ 只占课程名列，其它列不动")


def test_long_name_wrap_preserved():
    """超长课程名折行后完整保留（不截断、不加省略号）。"""
    app = _make_app()
    for W in (60, 80, 100):
        cols = app._columns(W)
        widths = [w for _k, _l, w in cols]
        starts = _col_starts(widths)
        name_i = [i for i, (k, _l, _w) in enumerate(cols) if k == "name"][0]
        name_w = widths[name_i]
        c = _COURSES[0]
        lines = app._course_lines(c, False, cols, cursor=False)
        joined = "".join(
            _extract(_plain(l), starts[name_i], name_w).rstrip()
            for l in lines)
        assert _LONG in joined.replace(" ", ""), "折行不应丢失/截断课程名"
    print("✓ 超长课程名折行完整保留（不截断不省略）")


def test_required_columns_present_and_name_readable():
    app = _make_app()
    for W in (50, 80, 120, 160):
        cols = app._columns(W)
        keys = [k for k, _l, _w in cols]
        for req in ("page", "no", "name", "seats", "avail"):
            assert req in keys, f"必保列 {req} 缺失 @{W}"
        name_w = next(w for k, _l, w in cols if k == "name")
        assert name_w >= 6, f"课程名列过窄：{name_w} @{W}"
    print("✓ 必保列总是存在；课程名列有可读最小宽")


def main() -> int:
    test_header_data_aligned_and_grid_contained()
    test_no_column_spills_past_width()
    test_cursor_confined_to_gutter()
    test_matched_star_confined_to_name_cell()
    test_long_name_wrap_preserved()
    test_required_columns_present_and_name_readable()
    print("=" * 60)
    print("表格布局不变量测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())