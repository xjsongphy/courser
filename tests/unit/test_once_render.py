"""`courser --once` 非 TUI 结果渲染测试（Rich Panel + Table）。

用法：uv run python tests/unit/test_once_render.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from io import StringIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from rich.console import Console  # noqa: E402

from courser.models import Course, RoundResult  # noqa: E402
from courser.tui.app import _render_once_result  # noqa: E402


def _render(r: RoundResult, width: int = 120) -> str:
    buf = StringIO()
    out = Console(file=buf, width=width, color_system=None)  # 非 TTY：去色、留框线
    _render_once_result(out, r)
    return buf.getvalue()


def test_once_success_with_matched():
    r = RoundResult(ok=True, pages=8, total=141)
    r.matched = [Course(course_no="02332976", name="《理想国》", category="通识课(通识核心课I)",
                        dept="哲学系", quota=100, selected=100, avail=0,
                        status="不可申请", page=2)]
    s = _render(r)
    assert "补退选空余名额" in s, "应有带标题的汇总 Panel"
    assert "页数 8" in s and "课程 141" in s and "命中 1" in s and "空余 0" in s
    assert "● 成功" in s, "成功状态应显示"
    assert "《理想国》" in s and "02332976" in s, "命中课程应进表格"
    print("✓ --once 成功渲染：Panel/汇总/命中表/成功状态")


def test_once_success_with_seats():
    r = RoundResult(ok=True, pages=3, total=40)
    r.matched = [Course(course_no="02130020", name="中国近代史", category="通识课(通识核心课I)",
                        dept="历史学系", quota=160, selected=159, avail=1,
                        status="可申请", page=3)]
    s = _render(r)
    assert "空余 1" in s, "空余数应体现"
    assert "可申请" in s
    print("✓ --once 有空余：空余计数 + 状态列")


def test_once_no_match_failure():
    s = _render(RoundResult(ok=False, pages=0, total=0))
    assert "● 失败" in s, "失败状态应显示"
    assert "本轮无命中课程" in s, "无命中应有提示而非空表"
    print("✓ --once 无命中失败：失败状态 + 无课程提示")


def main() -> int:
    test_once_success_with_matched()
    test_once_success_with_seats()
    test_once_no_match_failure()
    print("=" * 60)
    print("--once 结果渲染测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())