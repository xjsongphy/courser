"""终端状态的防御性复位（与 Textual 的 mouse 门禁配合）。

courser 的复制完全交给系统终端：用户在终端里用鼠标拖出原生 selection，
再 Cmd+C / Ctrl+Shift+C 复制。要做到这一点，关键约束是 **mouse reporting 必须
处于关闭**——否则终端把鼠标按钮/拖动事件发给应用，而不是建立原生 selection，
于是 Cmd+C 提示“没有在终端中选择要复制的内容”。

正常情况下入口用 `App.run(mouse=False)` 让 Textual 不接管鼠标（见
`courser/tui/app.py:main`）。但 Textual 的 LinuxDriver 有个细节：

    _enable_mouse_support()  和  _disable_mouse_support()
    两者都以 `self._mouse` 为门禁，mouse=False 时**都直接 return**。

也就是说，courser 自己不会去开启 mouse reporting，但 **也不会替我们清理**：
如果同一个终端里上一个异常退出的 TUI（或开发期跑挂的进程）把 DECSET
mouse mode（1000/1002/1003/1015/1006）留在 ON，courser 启动和退出都不会复位，
这一整轮原生拖选都会被吞。本模块就是兜底：启动前 / 退出后各主动发一次
关闭序列，把历史遗留的 mouse reporting 拉回 OFF。
"""

from __future__ import annotations

import sys

# 关闭 xterm mouse reporting（覆盖 Textual LinuxDriver 会开启的全部位：
# 1000 基本点击 · 1002 按钮拖动 · 1003 任意移动 · 1015 urxvt 高亮 ·
# 1006 SGR 扩展。1015 只在 urxvt 风格用，一并清掉最稳；1016 像素模式无
# 独立关闭位，同属 1000l 覆盖，无需单列。）
RESET_MOUSE = (
    "\x1b[?1000l"
    "\x1b[?1002l"
    "\x1b[?1003l"
    "\x1b[?1015l"
    "\x1b[?1006l"
)


def disable_terminal_mouse(stream=None) -> None:
    """向终端输出关闭 mouse reporting 的转义序列。

    只在 Textual 驱动尚未启动 / 已经退出时调用（即 `App.run(...)` 的前后），
    此时终端处于常规模式，直接写 stdout 即可到达 tty；驱动运行期间不应调用。
    无副作用：对已关闭 mouse 的干净终端是空操作，对遗留 ON 状态则完成复位。
    """
    out = stream or sys.stdout
    try:
        out.write(RESET_MOUSE)
        out.flush()
    except Exception:
        # 输出到非 tty（如被重定向/管道）时忽略，不因复位失败打断主流程。
        pass
