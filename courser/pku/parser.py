"""网页提取结果 → Course[] 的纯转换（无 IO、无浏览器，便于单元测试）。

输入是注入 JS（courser/pku/extract.py）返回并解析后的 dict，输出是领域对象。
这一层不调用 opencli、不发请求——纯函数式转换。
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Optional

from ..models import Course

_SEATS_RE = re.compile(r"(\d+)\s*[/／]\s*(\d+)")


def parse_seats(raw: str) -> tuple[Optional[int], Optional[int]]:
    """从「150 / 149」类文本解析 (限数, 已选)；解析不出则 (None, None)。"""
    m = _SEATS_RE.search(raw or "")
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _col(header: list[str], *keys: str) -> Optional[int]:
    for i, h in enumerate(header):
        if any(k in h for k in keys):
            return i
    return None


def _parse_course(header: list[str], cells: list[str], links: list[dict]) -> Course:
    c = Course()
    i_no = _col(header, "课程号")
    i_name = _col(header, "课程名")
    i_cat = _col(header, "课程类别")
    i_credit = _col(header, "学分")
    i_hours = _col(header, "周学时")
    i_teacher = _col(header, "教师")
    i_class = _col(header, "班号")
    i_dept = _col(header, "开课单位")
    i_grade = _col(header, "年级")
    i_sched = _col(header, "上课", "考试")
    i_pnp = _col(header, "P/NP")
    i_seats = _col(header, "限数")
    i_status = _col(header, "选课状态")

    def cell(i: Optional[int]) -> str:
        return cells[i] if i is not None and i < len(cells) else ""

    c.course_no, c.name = cell(i_no), cell(i_name)
    c.category, c.credits, c.weekly_hours = cell(i_cat), cell(i_credit), cell(i_hours)
    c.teacher, c.class_no, c.dept, c.grade = cell(i_teacher), cell(i_class), cell(i_dept), cell(i_grade)
    c.schedule, c.pnp = cell(i_sched), cell(i_pnp)
    c.seats_raw = cell(i_seats)
    quota, selected = parse_seats(c.seats_raw)
    if quota is not None:
        c.quota, c.selected = quota, selected
        c.avail = quota - selected
    c.status = cell(i_status)

    for l in links:
        h = l["h"] or ""
        if "goNested.do" in h:
            c.links["detail"] = h
            q = urllib.parse.parse_qs(urllib.parse.urlparse(h).query)
            if "course_seq_no" in q:
                c.seq = urllib.parse.unquote(q["course_seq_no"][0])
        elif "electSupplement.do" in h:
            c.links.setdefault("elect", []).append(h)   # 补选/刷新入口；监控时绝不点击
        elif "cancelCourse.do" in h:
            c.links["drop"] = h                          # 退选入口；同样只记录
    if not c.status and "elect" in c.links:
        c.status = "可申请" if c.links.get("elect_text") == "补选" else ""
    # 有些行以链接文本表达可申请/不可申请
    for l in links:
        if l["t"] in ("补选", "刷新") and "electSupplement.do" in (l["h"] or ""):
            c.status = c.status or ("可申请" if l["t"] == "补选" else "不可申请")
    return c


def pick_electable_table(tables: list[dict]) -> Optional[dict]:
    """挑出真正的「补退选可用列表」表格。

    页面可能同时出现：外层包裹表（表头长度异常）、已选上列表、可用列表。
    判定依据（由强到弱）：
    1. 表头长度在 10~16 之间（排除把整页包进去的畸形表）；
    2. 该表内包含翻页用的 Next 链接（分页只属于可用列表）；
    3. 含 electSupplement.do（补选/刷新）链接的行数最多。
    """
    sane = [t for t in tables if 10 <= len(t.get("header") or []) <= 16]
    if not sane:
        sane = tables
    for t in sane:
        if t.get("pager_here"):
            return t
    best, best_score = None, -1
    for t in sane:
        score = sum(
            1 for r in (t.get("rows") or [])
            if any("electSupplement" in (l.get("h") or "") for l in (r.get("links") or []))
        )
        if score > best_score:
            best, best_score = t, score
    return best if best_score > 0 else (sane[0] if sane else None)


def _sig(page_courses: list[Course]) -> str:
    """本页课程签名（用于翻页是否生效的判重）。"""
    head = page_courses[:3]
    return "|".join(c.key for c in head) + f"#{len(page_courses)}"


def parse_page(data: dict) -> tuple[list[Course], dict, bool]:
    """返回 (可用课程列表, 分页信息, 页面是否含风控提示语)。"""
    courses: list[Course] = []
    t = pick_electable_table(data.get("tables") or [])
    if t is not None:
        header, rows = t.get("header") or [], t.get("rows") or []
        for r in rows:
            courses.append(_parse_course(header, r.get("cells") or [], r.get("links") or []))
    pager = data.get("pager") or {}
    return (courses,
            {"has_next": bool(data.get("has_next")),
             "next_href": data.get("next_href"),
             "cur": (pager or {}).get("cur"),
             "total": (pager or {}).get("total")},
            bool(data.get("warning")))
