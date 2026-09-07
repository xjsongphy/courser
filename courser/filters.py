"""筛选规则匹配。

三个维度：课程名 / 课程类别 / 开课院系。
- 每个维度可以有多个条目；同一维度内为「或」匹配（子串包含即可）。
- 维度之间的组合由 match 决定：
    any  = 任一维度命中即算（默认，对应「英语系 或 通识课I类」这类需求）
    all  = 所有非空维度都必须命中
- 未配置任何筛选时，不命中任何课程（避免误报）。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Filters
from .fetch import Course


@dataclass
class FilterSet:
    filters: Filters = field(default_factory=Filters)

    def _hit(self, field_value: str, entries: list[str]) -> bool:
        if not entries:
            return False
        fv = field_value or ""
        return any(e and e in fv for e in entries)

    def matches(self, c: Course) -> bool:
        if self.filters.empty:
            return False
        groups_hit = []
        if self.filters.names:
            groups_hit.append(self._hit(c.name, self.filters.names))
        if self.filters.categories:
            groups_hit.append(self._hit(c.category, self.filters.categories))
        if self.filters.depts:
            groups_hit.append(self._hit(c.dept, self.filters.depts))
        if self.filters.match == "all":
            return all(groups_hit)
        return any(groups_hit)

    def matched(self, courses: list[Course]) -> list[Course]:
        return [c for c in courses if self.matches(c)]

    def describe(self) -> str:
        if self.filters.empty:
            return "（未配置筛选 → 不会告警）"
        mode = "满足任一条件" if self.filters.match != "all" else "满足全部条件"
        parts = [f"{k}: {' / '.join(v)}" for k, v in self.filters.active_groups]
        return f"{mode} | " + " ; ".join(parts) if parts else "（未配置筛选）"