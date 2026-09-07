"""筛选单元测试：FilterSet 任一/全部/空筛选 行为。

用法：uv run python tests/unit/test_filters.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser import filters  # noqa: E402
from courser.config import Filters  # noqa: E402
from courser.models import Course  # noqa: E402


def _mk_course(**kw) -> Course:
    base = dict(course_no="02030330", name="民俗学", category="通识课(通识核心课III)",
                dept="英语语言文学系", quota=150, selected=149, avail=1)
    base.update(kw)
    return Course(**base)


def test_filters_desc_and_match():
    f = Filters(names=["攀岩"], categories=["通识课(通选课III)"], match="any")
    fs = filters.FilterSet(f)
    c = _mk_course(name="攀岩", category="全校必修", dept="体育教研部")
    assert fs.matches(c), "任一命中：课程名命中即可"
    assert "任一" in fs.describe() or "任一" in fs.describe()
    f2 = Filters(names=["攀岩"], categories=["通识课(选)"], match="all")
    assert not filters.FilterSet(f2).matches(_mk_course(category="全校必修")), \
        "全部命中：类别不满足则应不中"
    # 空筛选 → 什么都不中（避免误报）
    assert filters.FilterSet(Filters()).matched([_mk_course()]) == []
    print("✓ filters：任一/全部/空筛选 行为正确；describe 含模式")


def test_multi_category_split():
    """同一门课可同时属于多类：『任选、思政选择性必修』拆成两类，
    选其中任一类都应命中该课；候选列表也应按独立类别去重。"""
    c = _mk_course(category="任选、思政选择性必修")
    assert c.categories == ["任选", "思政选择性必修"], c.categories
    # 只选其中一类即命中
    assert filters.FilterSet(Filters(categories=["任选"])).matches(c)
    assert filters.FilterSet(Filters(categories=["思政选择性必修"])).matches(c)
    # 不相关的类别不命中
    assert not filters.FilterSet(Filters(categories=["专业必修"])).matches(c)
    # 单类别课程不受影响
    single = _mk_course(category="通识课(通选课I)")
    assert single.categories == ["通识课(通选课I)"]
    assert filters.FilterSet(Filters(categories=["通识课(通选课I)"])).matches(single)
    print("✓ filters：多类别课程拆分/匹配正确")


def main() -> int:
    test_filters_desc_and_match()
    test_multi_category_split()
    print("=" * 60)
    print("filters 单元测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
