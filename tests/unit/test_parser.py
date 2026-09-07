"""parser / client 解析单元测试（不连浏览器）。

覆盖：从 EJECT_JS 同构的页面 JSON 解析出 Course（14 列，含学分/周学时/年级/状态）、
限选/空余/状态/seq、分页器信息、风控提示。
用法：uv run python tests/unit/test_parser.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.models import Course  # noqa: E402
from courser.pku import parser, client  # noqa: E402

# 与真实 EXTRACT_JS 输出同构的页面数据（14 列）
_PAGE = {
    "url": "https://elective.pku.edu.cn/elective2008/edu/pku/stu/elective/controller/supplement/supplement.jsp?netui_row=electableListGrid;0",
    "pager": {"cur": 1, "total": 2},
    "has_next": True,
    "next_href": "/elective2008/edu/pku/stu/elective/controller/supplement/supplement.jsp?netui_pagesize=electableListGrid%3B20&xh=2300011524&netui_row=electableListGrid%3B20",
    "warning": False,
    "tables": [{
        "header": ["课程号", "课程名", "课程类别", "学分", "周学时", "教师",
                   "班号", "开课单位", "年级", "上课/考试信息",
                   "自选P/NP", "限数/已选", "选课状态", "退选"],
        "rows": [
            {"cells": ["00433328", "近代物理实验 (II)", "通识课(通选课III)", "3.0",
                       "6.0", "蒋莹莹(副教授)", "8", "物理学院", "2024",
                       "1~16周 每周周一5~6节", "可申请", "12 / 12", "不可申请", ""],
             "links": [
                 {"t": "近代物理实验 (II)",
                  "h": "/elective2008/edu/pku/stu/elective/controller/supplement/goNested.do?course_seq_no=BZ202400433328_1"},
                 {"t": "补选",
                  "h": "/elective2008/edu/pku/stu/elective/controller/supplement/electSupplement.do?index=0&seq=BZ202400433328_1"}]},
            {"cells": ["02030330", "民俗学", "通识课(通识核心课III)", "2.0",
                       "2.0", "王娟(教授)", "1", "中国语言文学系", "",
                       "1~16周 每周周二5~6节", "可申请", "150 / 149", "可申请", ""],
             "links": [
                 {"t": "民俗学",
                  "h": "/elective2008/edu/pku/stu/elective/controller/supplement/goNested.do?course_seq_no=BZ202402030330_1"}]},
        ],
    }],
}


def test_parse_14cols():
    courses, pager, warned = parser.parse_page(_PAGE)
    assert len(courses) == 2, f"应解析出 2 门课，实得 {len(courses)}"
    c0, c1 = courses
    assert c0.course_no == "00433328" and c0.name == "近代物理实验 (II)"
    assert c0.category == "通识课(通选课III)" and c0.dept == "物理学院"
    assert c0.quota == 12 and c0.selected == 12 and c0.avail == 0, c0.seats_raw
    assert c0.status == "不可申请" and c0.seq == "BZ202400433328_1"
    assert c1.quota == 150 and c1.selected == 149 and c1.avail == 1, c1.seats_raw
    assert c1.name == "民俗学" and c1.category == "通识课(通识核心课III)"
    assert pager["cur"] == 1 and pager["total"] == 2 and pager["has_next"]
    assert warned is False
    assert _PAGE["next_href"] == pager["next_href"]
    print("✓ 解析：14 列页数据 → 课程对象字段/限选/空余/状态 正确")


def test_parse_seats_raw():
    assert parser.parse_seats("150 / 149") == (150, 149)
    assert parser.parse_seats("12/12") == (12, 12)
    assert parser.parse_seats("155／55") == (155, 55)   # 中文斜杠
    assert parser.parse_seats("—") == (None, None)
    assert parser.parse_seats("") == (None, None)
    print("✓ 限/选：英文斜杠、无空格、中文斜杠、缺失 解析正确")


def test_progress():
    seen = []
    p = client.Progress(lambda d, t, o: seen.append((d, t, o)))
    p.step("a")
    p.step("b", 10)
    p.step("c")
    assert p.done == 3 and p.total == 10
    assert seen[0] == (1, None, "a")
    assert seen[1] == (2, 10, "b")
    assert seen[2] == (3, 10, "c")
    print("✓ Progress：单调步进、total 稳定后作为分母")


def test_pager_row_filtered():
    """表格脚/分页栏不应被解析成课程（其含 JS 文本，渲染会崩）。"""
    from courser.models import is_real_course  # noqa: PLC0415
    real = Course(course_no="00433328", name="近代物理实验 (II)")
    assert is_real_course(real)
    fake = Course(course_no="Page 1 of 8 First / Previous Next / Last",
                  name="跳到： function doPagerSubmit(comp) { var form=...")
    assert not is_real_course(fake)
    assert not is_real_course(Course(course_no="", name="x"))
    # parse_page 过滤：含分页栏行的页只留真实课程
    data = {"pager": {"cur": 1, "total": 1}, "has_next": False, "warning": False,
            "tables": [{"header": ["课程号", "课程名", "限数/已选"], "rows": [
                {"cells": ["00433328", "近代物理实验", "12 / 12"], "links": []},
                {"cells": ["Page 1 of 8 First / Previous Next / Last",
                            "跳到： function doPagerSubmit(comp) {...}", ""], "links": []},
            ]}]}
    courses, _pager, _warned = parser.parse_page(data)
    assert len(courses) == 1 and courses[0].course_no == "00433328", \
        f"应只剩真实课程，实得 {[c.course_no for c in courses]}"
    print("✓ 表脚/分页栏不解析成课程（is_real_course 兜底）")


def main() -> int:
    test_parse_14cols()
    test_parse_seats_raw()
    test_progress()
    test_pager_row_filtered()
    print("=" * 60)
    print("parser 单元测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
