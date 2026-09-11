"""`courser --once` 的表现层（presentation adapter）。

职责：把「一轮抓取」的过程渲染成给人看的输出，**不改 Runner / PKU workflow**。
两种渲染路径：

- **TTY**：Rich ``Live`` 原地更新的状态区——已完成的阶段留成 ``✓`` 行，
  当前动作是一行会变的 ``spinner`` 行（如 ``⠹ 抓取  正在读取课程列表  4/7``），
  完成后整体替换成阶段摘要。默认只显示这些高层信息；``--verbose`` 才在下面
  追加逐条 workflow 明细。
- **非 TTY（重定向 / cron）**：保留逐行文本 + ``[courser]`` 前缀，方便 grep 与聚合。

配色沿用 TUI 语义色：cyan=当前动作/结构，绿=完成/成功，黄=告警，
红=失败，dim=次要 metadata；不要整句涂色。
"""

from __future__ import annotations

from typing import Optional

from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.markup import escape
from rich.text import Text

CYAN = "bold cyan"
GREEN = "bold green"
YELLOW = "bold yellow"
RED = "bold red"
DIM = "dim"

_SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

# 由 on_progress 的 op 文本归到少数给用户看的高层阶段（顺序即推进顺序）。
_PHASES = (
    ("登录", ("会话", "登录", "登出")),
    ("补退选", ("补退选", "返回", "已进入")),
    ("抓取", ("课程列表", "页")),
)
_PHASE_NAMES = [p[0] for p in _PHASES]

# 默认模式仍要"浮出"的日志：告警 / 失败 / 关键结果。其余 workflow 明细只在 --verbose 下显示。
# 注意：命中门数 / 无可新通知 已由最终 ✓ 筛选/通知 行覆盖，不在此重复。
_IMPORTANT_MARKERS = (
    "✗", "⚠", "失败", "风控", "验证码", "会话超时", "阻断",
    "邮件已发送", "发送上限", "重试上限",
    "重复页", "翻页失败", "已停止", "已取消", "刷课机", "浏览器桥不可用",
)


def _important(msg: str) -> bool:
    return any(k in msg for k in _IMPORTANT_MARKERS)


def _phase_of(op: str) -> str:
    for name, keys in _PHASES:
        if any(k in op for k in keys):
            return name
    return "抓取"


def _login_detail(mode: str) -> str:
    return {
        "reuse_session": "复用已登录会话",
        "login_click": "重新登录",
        "sso_auto": "SSO 直登",
    }.get(mode or "", mode or "自动填充")


def _pad(text: str, width: int) -> str:
    """按显示宽度（CJK 算两列）补齐空格，保证中文阶段标签也对齐。"""
    return text + " " * max(1, width - cell_len(text))


def _header() -> Text:
    t = Text("courser", style="bold")
    t.append("  单次抓取", style=DIM)
    return t


def _row(mark: str, mark_style: str, label: str, detail: str = "",
         detail_style: str = "") -> Text:
    """统一的行：`<mark> <阶段>   <详情>`——只给 mark/label 上色，详情保持默认。"""
    t = Text()
    t.append(f"{mark} ", style=mark_style)
    t.append(_pad(label, 10), style=CYAN)
    if detail:
        t.append(detail, style=detail_style or "")
    return t


class _TTYRenderer:
    """Rich Live 版：原地更新的阶段状态区。"""

    def __init__(self, console: Console, verbose: bool):
        self.console = console
        self.verbose = verbose
        self.phases: list[tuple[str, str]] = []   # 已完成的 (阶段, 详情)
        self.current: Optional[tuple[str, str]] = None
        self.details: list[Text] = []             # 明细/重要日志（verbose 全收，默认只收 important）
        self._last_phase_idx = -1
        self._tick = 0
        self._live = Live(console=console, refresh_per_second=10,
                          vertical_overflow="visible", auto_refresh=True)
        self._live.start()

    # -- 输入 -----------------------------------------------------------
    def log(self, msg: str) -> None:
        msg = str(msg)
        if self.verbose:
            self.details.append(Text(escape(msg), style=DIM))
        elif _important(msg):
            style = RED if any(k in msg for k in ("✗", "失败", "阻断")) else (
                YELLOW if any(k in msg for k in ("⚠", "风控", "验证码", "超时", "重复页")) else "")
            self.details.append(Text(escape(msg), style=style))
        else:
            return  # 默认模式静默 workflow 明细
        self.details = self.details[-8:]  # 明细区有界，避免刷屏
        self._refresh()

    def progress(self, done: Optional[int], total: Optional[int], op: str) -> None:
        op = str(op)
        phase = _phase_of(op)
        idx = _PHASE_NAMES.index(phase) if phase in _PHASE_NAMES else len(_PHASE_NAMES) - 1
        # 阶段推进：把刚离开的阶段留成 ✓ 行
        if idx > self._last_phase_idx and self.current is not None:
            self.phases.append(self.current)
        self._last_phase_idx = idx
        self.current = (phase, op)
        self._tick += 1
        self._refresh()

    # -- 渲染 -----------------------------------------------------------
    def _render(self) -> RenderableType:
        lines: list[RenderableType] = [_header(), Text("")]
        for label, detail in self.phases:
            lines.append(_row("✓", GREEN, label, detail))
        if self.current is not None:
            name, op = self.current
            spin = _SPIN[self._tick % len(_SPIN)]
            t = Text()
            t.append(f"{spin} ", style=CYAN)
            t.append(_pad(name, 10), style=CYAN)
            t.append(op)
            lines.append(t)
        if self.details:
            lines.append(Text(""))
            lines.extend(self.details)
        return Group(*lines)

    def _refresh(self) -> None:
        self._live.update(self._render())

    # -- 收尾 -----------------------------------------------------------
    def finish(self, r) -> None:
        """把 Live 最后一帧替换成「阶段摘要 + 重要明细」后停止。"""
        lines = list(_summary_lines(r))
        if self.details:
            lines.append(Text(""))
            lines.extend(self.details)
        self._live.update(Group(*lines))
        self._live.stop()

    def stop(self) -> None:
        try:
            self._live.stop()
        except Exception:
            pass


class _PlainRenderer:
    """非 TTY：逐行文本 + [courser] 前缀（保持 grep / cron 友好）。"""

    def __init__(self, console: Console, verbose: bool):
        self.console = console
        self.verbose = verbose

    def log(self, msg: str) -> None:
        self.console.print(f"\\[courser] {escape(str(msg))}", soft_wrap=True)

    def progress(self, done: Optional[int], total: Optional[int], op: str) -> None:
        self.console.print(f"\\[courser] [dim]{escape(str(op))}[/dim]", soft_wrap=True)

    def finish(self, r) -> None:  # 结果由 _render_once_result 负责
        pass

    def stop(self) -> None:
        pass


def make_once_renderer(err: Console, out: Console, verbose: bool):
    """TTY（stderr 是终端）→ Live 渲染；否则逐行文本。"""
    if err.is_terminal:
        return _TTYRenderer(err, verbose)
    return _PlainRenderer(err, verbose)


def _summary_lines(r) -> list[RenderableType]:
    """阶段摘要行（header + ✓/✗ 行）。供 finish / 测试复用。"""
    lines: list[RenderableType] = [_header(), Text("")]
    if r is not None and getattr(r, "ok", False):
        seats = sum(1 for c in (r.matched or []) if getattr(c, "has_seats", False))
        lines.append(_row("✓", GREEN, "登录", _login_detail(getattr(r, "login_mode", ""))))
        lines.append(_row("✓", GREEN, "抓取", f"{r.pages} 页 · {r.total} 门"))
        lines.append(_row("✓", GREEN, "筛选",
                          f"{len(r.matched or [])} 门 · {seats} 门有空余"))
        notify = f"已发送 {len(r.notified)} 封" if r.notified else "无可新通知"
        lines.append(_row("✓", GREEN, "通知", notify))
        if getattr(r, "warning_hit", False):
            lines.append(_row("⚠", RED, "风控", "本轮命中刷课机警告"))
    else:
        err = (getattr(r, "error", "") or "本轮失败") if r is not None else "本轮失败"
        lines.append(_row("✗", RED, "本轮失败", first_line(err), detail_style=RED))
    return lines


def render_summary_from_result(r) -> RenderableType:
    """给测试/复用：由 RoundResult 生成阶段摘要渲染体（与 TTY.finish 一致）。"""
    return Group(*_summary_lines(r))


def first_line(text: str) -> str:
    line = str(text).strip().splitlines()[0] if str(text).strip() else ""
    return line[:120]


__all__ = ["make_once_renderer", "_TTYRenderer", "_PlainRenderer", "render_summary_from_result"]
