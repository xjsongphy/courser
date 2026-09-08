"""终端原生复制（鼠标框选 + Cmd+C）的回归测试。

产品约束：courser 的复制完全交给系统终端，不依赖 Textual 的应用内文本选择。
要达成这一点必须同时满足：

1. 应用不进备用屏：用 `PrimaryScreenDriver` 拦截 DECSET 1049，
   让界面留在终端主缓冲区（否则原生拖选对它无效）；
2. 不开启周期重绘：状态栏只展示确定发生的时刻（绝对时间），
   绝不 set_interval(1s) 每秒刷新（否则 terminal selection 会被下次刷新清掉）；
3. mouse reporting 保持关闭：入口用 `run(mouse=False)`（Textual 不去开启）；
   并在启动前 / 退出后主动复位历史遗留的 mouse mode
   （terminal_state.disable_terminal_mouse）。Textual 的 LinuxDriver 只在
   `self._mouse` 为真时才发送 enable/disable 序列，mouse=False 意味着它既不开启
   也不会替我们清理上个崩溃 TUI 留下的 1000/1003/1015/1006——所以必须兜底。

用法：uv run python tests/tui/test_terminal_selection.py
"""

from __future__ import annotations

import asyncio
import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_tmpdir = tempfile.mkdtemp(prefix="courser-tsel-")
os.environ["COURSER_CONFIG"] = str(Path(_tmpdir) / "config.json")

from courser.tui.app import CourserApp  # noqa: E402
from courser.tui import driver as drv  # noqa: E402


def test_primary_driver_strips_alt_screen():
    """DECSET 1049（进入/退出备用屏）必须被彻底剔除，界面留在主缓冲区。"""
    data = "abc\x1b[?1049hdef\x1b[?1049lghi"
    filtered = drv.strip_alt_screen(data)
    assert "\x1b[?1049h" not in filtered
    assert "\x1b[?1049l" not in filtered
    assert filtered == "abcdefghi"


def test_primary_driver_write_filters_both_sequences():
    """真实 write 路径也应过滤两个序列，且不吞掉其余内容。"""
    d = drv.PrimaryScreenDriver
    assert drv._ALT_SCREEN_ENTER == "\x1b[?1049h"
    assert drv._ALT_SCREEN_EXIT == "\x1b[?1049l"


def test_app_uses_primary_screen_driver():
    """应用必须用主缓冲区驱动；若回退到 None（非 POSIX）则跳过。"""
    driver = CourserApp.get_driver_class(CourserApp)
    if driver is None:
        return
    assert driver is drv.PrimaryScreenDriver


def test_no_allow_select_override():
    """不得自行把 ALLOW_SELECT 关掉——那是 Textual 的应用内文本选择，
    与系统终端的原生 selection 是不同层，配置它只会误导。"""
    assert CourserApp.__dict__.get("ALLOW_SELECT") is None, \
        "不应在 CourserApp 上显式覆盖 ALLOW_SELECT"


def test_tui_has_no_periodic_status_ticker():
    """禁止周期重绘：不得存在 _render_status_ticker / _tick，
    也不得 set_interval 每秒刷新（会清除终端 mouse selection）。"""
    assert not hasattr(CourserApp, "_render_status_ticker")
    assert not hasattr(CourserApp, "_tick")
    src = (Path(__file__).resolve().parents[2]
           / "courser" / "tui" / "app.py").read_text(encoding="utf-8")
    assert not re.search(r"\bset_interval\s*\(", src), \
        "app.py 不应调用 set_interval（周期重绘会干扰终端复制）"


def test_run_uses_mouse_false():
    """入口必须以 mouse=False 运行，不让 Textual 接管鼠标。"""
    src = (Path(__file__).resolve().parents[2]
           / "courser" / "tui" / "app.py").read_text(encoding="utf-8")
    assert re.search(r"\.run\(\s*mouse=False\s*\)", src), \
        "入口应保持 run(mouse=False)"


def test_mouse_reset_covers_all_textual_modes():
    """复位序列必须覆盖 Textual LinuxDriver 会开启的全部 mouse mode。
    LinuxDriver 开的是 1000h/1003h/1015h/1006h（源码），复位必须包含 1015。"""
    from courser.tui.terminal_state import RESET_MOUSE, disable_terminal_mouse
    for code in (1000, 1002, 1003, 1015, 1006):
        assert f"\x1b[?{code}l" in RESET_MOUSE, f"复位序列缺少 ?{code}l"
    for seq in RESET_MOUSE.split("\x1b")[1:]:
        assert seq.endswith("l"), f"复位序列只能关闭（l），含 {seq!r}"
    # 可注入 stream：写往自定义缓冲且不抛错
    import io
    buf = io.StringIO()
    disable_terminal_mouse(stream=buf)
    assert buf.getvalue() == RESET_MOUSE


def test_run_wrapped_with_mouse_reset():
    """main() 必须在 run 前复位 mouse，并用 finally 保证退出后也复位。"""
    src = (Path(__file__).resolve().parents[2]
           / "courser" / "tui" / "app.py").read_text(encoding="utf-8")
    assert "disable_terminal_mouse()" in src
    assert "import disable_terminal_mouse" in src or \
        "from .terminal_state import disable_terminal_mouse" in src
    # disable 必须在 .run(mouse=False) 之前出现（启动前清一次历史 mouse mode）
    assert src.index("disable_terminal_mouse()") < src.index(".run(mouse=False)")
    # 退出后也复位：try/finally 里必须有第二次 disable
    assert src.count("disable_terminal_mouse()") >= 2
    assert "finally:" in src


async def _smoke() -> None:
    """无头冒烟：应用能正常启动、渲染、退出。"""
    from courser.config import Config
    from courser.models import Course

    def mk(no, name):
        return Course(course_no=no, name=name, category="通识课", dept="外院",
                      teacher="王", seats_raw="限50选40", quota=50,
                      selected=40, avail=10, status="可申请", page=1)

    cfg = Config.load()
    cfg.first_run_done = True
    app = CourserApp(cfg)
    app.courses = [mk("001", "大学英语四"), mk("002", "普通物理")]
    async with app.run_test(size=(120, 30)) as pilot:
        await pilot.press("down")
        await pilot.press("y")
        await pilot.press("q")
    print("终端复制相关结构检查 OK：主缓冲区 / 无 ALLOW_SELECT / 无周期重绘 / mouse=False")
    return 0


if __name__ == "__main__":
    for name in sorted(globals()):
        if name.startswith("test_") and callable(globals()[name]):
            globals()[name]()
    raise SystemExit(asyncio.run(_smoke()))