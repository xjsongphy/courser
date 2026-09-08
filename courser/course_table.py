"""课程表规范 schema + 单元格格式化（唯一来源）。

课程表是所有输出渠道共享的**核心展示对象**，这里确立它的 canonical 定义：

    Course
       │
       ▼
   COURSE_COLUMNS（列定义） + course_display（字段→显示文本）
       │
   ┌───┴─────────────┐
   ▼                 ▼
 TUI 列布局          Email(HTML/纯文本)
 终端宽度/折行/颜色   HTML/markdown 标签

各渠道**共享**：列 key / 标题 / 对齐语义 / 数据格式化（course_display）。
各渠道**不共享**：终端宽度、折行规则、ANSI 颜色、HTML/markdown 标签。

这样『限选/已选 拆分』『空余右对齐』只改这一处，TUI 主表 / TUI 筛选表 /
邮件 HTML / 邮件纯文本 自动同步，杜绝各模块手写字符串再分裂。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .models import Course


@dataclass(frozen=True)
class Column:
    """一列课程表的规范定义。

    - 结构列（页数/课程号/限选/已选/空余）：width 固定；weight 为 0。
    - 文本列（课程名/类别/开课单位/教师）：width=None，按 weight 弹性分配。
    """

    key: str
    title: str
    align: str = "left"
    width: Optional[int] = None      # 固定宽（结构列/数值列）
    min_width: int = 0               # 弹性文本列的底线
    weight: float = 0.0              # 弹性文本列的宽度权重
    preferred: Optional[int] = None  # 弹性文本列的 cap

    @property
    def is_structural(self) -> bool:
        """结构列（固定宽，数值/ID），文本列（弹性）的对立面。"""
        return self.width is not None


# 课程表规范顺序：页数 → 空余。TUI 实际渲染会把文本列塞进中间
# （页数/课程号, [文本列…], 限选/已选/空余）；这是终端布局 adapter 的决定，
# 仍在 _columns() 里基于本 schema 推导，不另写第二份列定义。
COURSE_COLUMNS: list[Column] = [
    Column("page", "页数",        align="right",  width=4),
    Column("no", "课程号",        align="left",   width=10),
    Column("name", "课程名",      align="left",   min_width=12, weight=3.0, preferred=30),
    Column("cat", "课程类别",     align="center", min_width=8,  weight=1.5, preferred=20),
    Column("dept", "开课单位",    align="left",   min_width=8,  weight=1.5, preferred=20),
    Column("teacher", "教师",     align="left",   min_width=10, weight=2.0, preferred=26),
    Column("quota", "限选",       align="right",  width=5),
    Column("selected", "已选",    align="right",  width=5),
    Column("avail", "空余",       align="right",  width=5),
]

# key → Column 的便捷查找
_COLUMN_BY_KEY: dict[str, Column] = {c.key: c for c in COURSE_COLUMNS}


def column(key: str) -> Optional[Column]:
    """按 key 取列定义；未知 key 返回 None。"""
    return _COLUMN_BY_KEY.get(key)


def course_display(c: Course, key: str) -> str:
    """课程某一列的**显示文本**（canonical 格式化）。

    所有渠道都从这里取值，保证格式一致：
    - 数值列：None/未知 → '—'，有值 → str
    - 文本列：空 → '—'
    邮件额外用到的课程字段（班号/上课信息）也归入本表单。
    """
    if key == "page":
        return str(c.page) if c.page else "—"
    if key == "no":
        return c.course_no or "—"
    if key == "name":
        return c.name or "—"
    if key == "cat":
        return c.category or "—"
    if key == "dept":
        return c.dept or "—"
    if key == "teacher":
        return c.teacher or "—"
    if key == "class_no":
        return c.class_no or "—"
    if key == "schedule":
        return c.schedule or "—"
    if key == "quota":
        return str(c.quota) if c.quota is not None else "—"
    if key == "selected":
        return str(c.selected) if c.selected is not None else "—"
    if key == "avail":
        return str(c.avail) if c.avail >= 0 else "—"
    return ""


def course_order_key(c: Course) -> tuple[int]:
    """选课网顺序：页码升序；同页保持原始列表顺序。"""
    return (c.page if c.page > 0 else 10**9,)


def ordered_courses(courses: list[Course]) -> list[Course]:
    """按选课网顺序返回课程副本；Python 稳定排序保留同页上下顺序。"""
    return sorted(courses, key=course_order_key)


def seats_display(c: Course) -> str:
    """限选/已选 合并串（邮件纯文本等散文场景用）；缺失回退原始串。"""
    if c.quota is not None and c.selected is not None:
        return f"{c.selected}/{c.quota}"
    return (c.seats_raw or "").replace(" ", "") or "—"
