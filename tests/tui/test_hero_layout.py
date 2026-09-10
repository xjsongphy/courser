"""Hero 布局 + 课程表对齐回归测试（无头）。

覆盖：
- Hero：宽屏左右两栏（含 │ 分隔）/ 窄屏单栏；含 筛选 / 通知 / 最近抓取 / 数据
- 最近抓取用紧凑时间（今天 → HH:MM），数据保留「N 页 · M 门课程」
- 数量型列右对齐 / ID·名称左对齐；限/已选规范化成 `30/12`（无空格）

用法：uv run python tests/tui/test_hero_layout.py
"""

from __future__ import annotations

import asyncio
import io
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from rich.console import Console  # noqa: E402

from courser.config import Config  # noqa: E402
from courser.models import Course, RoundResult  # noqa: E402
from courser.tui.app import CourserApp  # noqa: E402

COURSES = [
    Course(course_no="00431563", name="天体物理观测实验", category="任选",
           dept="物理学院", teacher="张老师", quota=30, selected=12, avail=18,
           seats_raw="30 / 12", seq="a", page=1, status="可申请"),
    Course(course_no="02131580", name="中美关系史", category="通识课",
           dept="历史学系", teacher="李老师", quota=150, selected=15, avail=150,
           seats_raw="150/15", seq="b", page=2, status="可申请"),
]


def _renderable_text(rv, width: int) -> str:
    if hasattr(rv, "_renderable"):
        buf = io.StringIO()
        Console(force_terminal=False, color_system=None, width=width,
                file=buf).print(rv._renderable, end="")
        return buf.getvalue()
    return str(rv)   # Content（markup 字符串）→ 纯文本


async def _capture(w: int, h: int) -> tuple[str, str]:
    cfg = Config()
    cfg.first_run_done = True
    cfg.filters.depts = ["物理学院"]
    app = CourserApp(cfg)
    async with app.run_test(size=(w, h)) as p:
        await p.pause(0.2)
        app.courses = COURSES
        app.main.snapshot_ts = "2026-09-07 19:39:41"
        app.main.snapshot_meta = "8 页 · 152 门课程"
        app.main.risk_summary = (5, "低", 5, 100)
        app._render_main(force=True)
        await p.pause(0.2)
        hero = _renderable_text(app.query_one("#hero").render(), w - 4)
        body = _renderable_text(app.query_one("#courselist").render(), w - 4)
        return hero, body


def _plain_lines(s: str) -> list[str]:
    return [ln for ln in s.splitlines() if ln.strip()]


async def test_hero_layout() -> None:
    # 宽屏：左右两栏（筛选/通知 与 最近抓取/数据 同一行）；含四个字段 + 标题
    hero, _ = await _capture(120, 30)
    assert "courser" in hero and "PKU 补退选空余名额监控" in hero
    assert any("筛选" in ln and "最近抓取" in ln for ln in hero.splitlines()), \
        "宽屏应左右两栏：筛选 与 最近抓取 同在一行"
    for field in ("筛选", "通知", "最近抓取", "数据", "风控"):
        assert field in hero, f"Hero 缺字段 {field}"
    assert "19:39" in hero, "最近抓取应为紧凑时间（同一天 HH:MM）"
    assert "8 页 · 152 门课程" in hero, "数据字段应含抓取规模"
    assert "5%" in hero and "5/100" in hero, "Hero 应显示风控 5%（5/100）"
    assert "开课院系×1" in hero, "筛选摘要应含开课院系计数"
    # 窄屏：退化为单栏（筛选 与 最近抓取 各占一行）
    hero80, _ = await _capture(80, 24)
    assert not any("筛选" in ln and "最近抓取" in ln for ln in hero80.splitlines()), \
        "窄屏应单栏：筛选 与 最近抓取 不在同一行"
    assert "筛选" in hero80 and "最近抓取" in hero80


async def test_table_alignment_and_format() -> None:
    app = CourserApp(Config())   # 纯方法断言，不启动 run_test
    assert app._col_justify("page") == "right"
    assert app._col_justify("quota") == "right"
    assert app._col_justify("selected") == "right"
    assert app._col_justify("avail") == "right"
    assert app._col_justify("no") == "left"
    assert app._col_justify("name") == "left"
    cols = app._columns(120)
    keys = [k for k, _l, _w in cols]
    assert "quota" in keys and "selected" in keys and "avail" in keys
    assert "seats" not in keys, "限选/已选 合并列应拆开"


async def test_risk_unknown_and_high():
    """风控未知/无样本 → '—'；高/极高 → 红色；0% → 绿色（不崩溃）。"""
    app = CourserApp(Config())
    app.main.risk_summary = None
    assert "—" in app._risk_value_markup()
    app.main.risk_summary = (0, "无", 0, 0)
    assert "—" in app._risk_value_markup(), "无样本应对显示未知"
    app.main.risk_summary = (100, "极高", 10, 10)
    m = app._risk_value_markup()
    assert "100%" in m and "red" in m
    app.main.risk_summary = (0, "无", 0, 10)
    assert "0%（0/10）" in app._risk_value_markup()


async def test_risk_summary_updates_without_courses():
    """风控命中而本轮无课程（典型的阻断场景）时，Hero 风控也必须更新为最新历史。
    回归点：不能只在 `r.ok and r.courses` 时才更新（否则重启后显示旧 last_round）。"""
    cfg = Config()
    cfg.first_run_done = True
    app = CourserApp(cfg)
    async with app.run_test(size=(120, 30)) as p:
        await p.pause(0.2)
        app.main.risk_summary = (0, "无", 0, 8)   # 旧值（例如上次 0/8）
        # 本轮：真的触发风控、没有课程数据（警告阻断页）
        r = RoundResult(ok=True, pages=0, courses=[], warning_hit=True,
                        risk_percent=11, risk_label="中", risk_hits=1, risk_total=9)
        app._apply_round(r)
        assert app.main.risk_summary == (11, "中", 1, 9), \
            "本轮无课程也必须刷新风控摘要为 1/9，不能停在上次 0/8"
        await p.pause(0.2)
    print("✓ Hero：风控命中且本轮无课程 → risk_summary 仍更新（1/9）")


async def main() -> int:
    await test_hero_layout()
    await test_table_alignment_and_format()
    await test_risk_unknown_and_high()
    await test_risk_summary_updates_without_courses()
    print("Hero / 表格对齐回归：两栏·窄屏单栏·筛选通知·最近抓取·数据·风控·右对齐·拆分限选/已选 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))