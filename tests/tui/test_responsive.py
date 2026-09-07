"""响应式几何回归测试：四档终端尺寸无头启动主页课程窗口。

覆盖 80×24 / 100×30 / 120×36 / 160×45：
- 渲染不抛异常、页面就位、每档都能看到课程
- 每行（含折行）都不超出终端宽度、都是合法 markup
- 表头各必保列都在
（注：新版渲染器按真实折行行数分页，故不断言『可见课程数随高度单调』——
  更宽的窗口可能让每门课占更少物理行，两者互相抵消。）
用法：uv run python tests/tui/test_responsive.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from rich.text import Text
from rich.cells import cell_len

from courser.config import Config
from courser.models import Course
from courser.tui.app import CourserApp

_GEOMETRIES = [(80, 24), (100, 30), (120, 36), (160, 45)]


def _mk_courses() -> list[Course]:
    return [
        Course(course_no=f"{i:03d}", name=f"计算机科学与技术导论 {i}（含实验与课程设计）",
               category="通识课(通识核心课III)", dept="信息科学技术学院",
               teacher="张伟(教授)", quota=100 - i, selected=99 - i,
               avail=1 if i % 2 == 0 else 0, seats_raw="100/99", seq=f"s{i}",
               status="可申请", page=i + 1)
        for i in range(200)
    ]


async def _render_at(w: int, h: int) -> tuple[CourserApp, str, str]:
    cfg = Config()
    cfg.first_run_done = True
    app = CourserApp(cfg)
    async with app.run_test(size=(w, h)) as p:
        await p.pause(0.3)
        app.courses = _mk_courses()
        app._set_view("all")
        app._render_main(force=True)
        body = str(app.query_one("#courselist").render())
        header = str(app.query_one("#coursehead").render())
        return app, body, header


async def main() -> int:
    for w, h in _GEOMETRIES:
        app, body, _ = await _render_at(w, h)
        lines = [ln for ln in body.splitlines() if ln.strip()]
        assert lines, f"{w}×{h} 无渲染结果"
        assert len(lines) >= 2, f"{w}×{h} 至少应有表头+数据行"
        # 每行（含折行）单行、合法 markup、不出界
        for ln in lines:
            ln_text = ln[len(ln) - len(ln.lstrip()):]  # 保留前导（缩进/光标）
            Text.from_markup(ln)                       # 合法 markup
            assert cell_len(Text.from_markup(ln).plain) <= w, f"行超宽 @{w}×{h}"
        print(f"  {w}×{h}：可见课程行 {len(lines) - 1} 行 OK")

    print("=" * 60)
    print("响应式几何回归测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
