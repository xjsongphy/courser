"""Textual driver that stays in the terminal primary buffer.

courser 的复制完全交给系统终端：用户用鼠标在终端里拖动建立原生 selection，
再用 Cmd+C / Ctrl+Shift+C 复制。要实现这一点必须满足两个前提，缺一不可：

1. Textual 不接管鼠标（`App.run(mouse=False)`，见 `courser/tui/app.py:main`）；
2. 应用不进入 alternate screen——否则终端把界面画在隔离缓冲区里，
   原生拖选对它无效。

Textual 的 `LinuxDriver` 默认会在启动/退出时发送 DECSET 1049 进入/退出备用屏。
本模块用一个 `LinuxDriver` 子类拦截并丢弃这两个转义序列，让界面始终停留在
主缓冲区（primary buffer），这样 VS Code / iTerm / Terminal.app 的原生
鼠标拖选 + 复制就能正常工作。
"""

from __future__ import annotations

from textual.drivers.linux_driver import LinuxDriver

_ALT_SCREEN_ENTER = "\x1b[?1049h"  # DECSET 1049：进入 alternate screen
_ALT_SCREEN_EXIT = "\x1b[?1049l"   # 退出 alternate screen


def strip_alt_screen(data: str) -> str:
    """从一段转义序列里剔除进入/退出备用屏的 DECSET 1049。"""
    return (data
            .replace(_ALT_SCREEN_ENTER, "")
            .replace(_ALT_SCREEN_EXIT, ""))


class PrimaryScreenDriver(LinuxDriver):
    """Textual driver that stays in the terminal primary buffer.

    让底层终端能够用鼠标建立原生 selection，从而支持 Cmd+C 复制。
    """

    def write(self, data: str) -> None:
        data = strip_alt_screen(data)
        if data:
            super().write(data)