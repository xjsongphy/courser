"""courser 的纯文本监控 TUI。

定位：一个 **persistent status monitor**，而不是塞进终端的桌面应用。主页在
常态下只呈现当前状态、关注的课程与最近一次事件；所有配置都通过少量二级页面
完成。页面用线条边框面板分区，配色克制（统一原语见 courser/tui/theme.py）。

主页分三层，职责不重叠：顶部 Hero 稳定摘要（产品身份 + 筛选/通知/上次发信/最近
发送/最近抓取/数据规模），中部课程列表，底部一行实时活动状态。底部在抓取/监控
进行中每秒刷新（已用秒数实走），空闲不重绘；用自调度 timer，绝不 set_interval。

文本选择与复制完全交给 Textual 自己：`run(mouse=True)`，用户鼠标拖动即形成
Textual 内置 selection，松手（TextSelected）自动把选中纯文本经 OSC 52 写入剪贴板
并底部 toast 提示；同时恢复 Settings / Help / 日志 / 详情页的滚轮滚动。不再依赖
终端原生 selection（见 tests/tui/test_text_selection.py）。

交互规范（一种操作一种入口，一个键一种语义）：
- 鼠标拖动 = 选择文字，松手 = 自动复制；滚轮 = 滚当前页 viewport（不改光标）；
- Enter = 进入 / 确认；Esc = 返回 / 放弃（**Esc 任何地方都不保存**）；
- ↑↓ = 移动，Space = 开始/停止或选中/取消（各页内遵循）；
- 主页键：Space 开始/停止 · r 立即抓取 · / 按列查找 · f 筛选 · s 设置 ·
  l 日志 · 1/2/3 视图（全部/符合筛选/只看空余）· h 帮助 · q 退出。
所有文字输入（设置字段、首启收件邮箱、查找词等）都复用同一套行内编辑器
（见 courser/editing.py）：值保留在原行、回车进入、←/→ 移动光标、回车确认、
Esc 取消；没有独立输入框。筛选页的搜索则直接输入即过滤。
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import time
from datetime import datetime, timedelta
from math import ceil
from typing import Optional

from rich.cells import cell_len
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from textual import on, events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.geometry import Region
from textual.widgets import Static

from .. import notifier
from ..config import Config, load_env_file
from ..editing import FieldEditor
from ..filters import FilterSet
from ..models import Course, FetchFailureKind, RoundResult
from ..scheduler import MonitorScheduler
from ..risk import RiskHistory, risk_label
from ..course_table import (COURSE_COLUMNS, course_display, ordered_courses,
                           seats_display)
from ..storage import RISK_FILE, SnapshotStore
from .state import (EditingState, FilterViewState, MainViewState,
                    ProgressState, SettingsViewState)
from .once_render import make_once_renderer
from .theme import (ACCENT, COURSE_DETAIL_FIELDS, CSS, GROUPS, SEP, SETTINGS_FIELDS,
                    FocusScroll, FocusableStatic, _hint, _pad,
                    field_mutate, field_value, kv_row, page_hint, plain_markup,
                    risk_markup, shortcut_row, ui_accent, ui_error, ui_key,
                    ui_label, ui_meta, ui_ok, ui_section, ui_title, ui_value,
                    ui_warn)

# 为兼容旧内部命名（保持渲染/按键逻辑可读性，缩写映射到 theme/state 的统一实现）
_risk_markup = risk_markup
_kv_row = kv_row
_shortcut_row = shortcut_row
_page_hint = page_hint
_plain_markup = plain_markup
_field_value = field_value
_field_mutate = field_mutate

# 全局活动状态栏（底部唯一 activity 行）展示的页面；其它页面隐掉，正文各自负责。
_ACTIVITY_PAGES = {"main", "filters", "settings", "logs", "detail"}
# 全局底部操作栏（#keys）展示的页面：与 activity 同，都是有稳定底部操作提示的页面。
_KEYS_PAGES = {"main", "filters", "settings", "logs", "detail", "setup"}

def _compact_dt(dt: datetime) -> str:
    """把时间压成随年龄递减的紧凑格式：今天 HH:MM；昨天 "昨天 HH:MM"；
    更久 "MM-DD HH:MM"；跨年 "YYYY-MM-DD HH:MM"。完整时间戳留给日志页。"""
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    if dt.date() == (now - timedelta(days=1)).date():
        return f"昨天 {dt.strftime('%H:%M')}"
    if dt.year == now.year:
        return dt.strftime("%m-%d %H:%M")
    return dt.strftime("%Y-%m-%d %H:%M")


_measure_console = Console(force_terminal=False)


def _console_measure(renderable) -> int:
    """量出 renderable 的自然宽度（格）。用于判断两栏是否放得下而不溢出。"""
    try:
        return _measure_console.measure(renderable).minimum
    except Exception:
        return 0


# 单元格对齐 / 是否折行：统一来自 canonical schema（courser/course_table.py），
# 这里不再散落手写列定义，避免与邮件/其它渠道分裂。
def _column_def(key: str):
    from ..course_table import column
    return column(key)


def _col_justify_for(key: str) -> str:
    col = _column_def(key)
    return col.align if col else "left"


def _col_no_wrap(key: str) -> bool:
    """结构列（固定宽数值/ID）绝不折行；文本列可折行。"""
    col = _column_def(key)
    return bool(col and col.is_structural)


# 顶部 hero 两栏布局：左右两栏之间的分隔区宽度（竖线 + 间距）。
# 改这里必须同步更新 _render_hero 的自适应判定式（(inner - _HERO_SEP)//2）。
_HERO_SEP = 5


class CourseViewport(Static):
    """课程区容器：当它自身尺寸变化（resize / 搜索开合 / 首行高度改变）时，
    让应用重填课程——保证 Textual 的真实布局永远是唯一的 viewport 来源。
    """

    can_focus = False

    def on_resize(self, event: events.Resize) -> None:
        app = getattr(self, "app", None)
        if app is not None and app.is_mounted:
            app._schedule_course_render()


class CourserApp(App):
    """纯文本菜单 TUI：主页面 + 筛选/设置/日志/帮助/详情/首启设置。"""

    TITLE = "courser"
    SUB_TITLE = "PKU 补退选空余名额监控"
    CSS = CSS
    BINDINGS = [Binding("ctrl+c", "quit", "退出", show=False, priority=True)]

    # 测试可通过覆盖注入：把验证码确认窗口 / 结果自动清除的超时常压缩短，
    # settings 测试就不必真等 2 秒（否则每次仅这两处就白耗 ~4.4s）。
    TEST_MAIL_CONFIRM_TIMEOUT_S = 2.0   # 首回车后等二次确认的默认窗口
    TEST_MAIL_RESULT_CLEAR_S = 2.0       # 发送结果展示后自动清除的默认时长

    def __init__(self, cfg: Config):
        # 空白区域使用终端自身的默认背景/调色板，不铺 Textual 深色主题底。
        super().__init__(ansi_color=True)
        self.cfg = cfg
        self._shutting_down = False
        self._shutdown_message: Optional[str] = None
        self.page = "main"
        self.log_buf: list[str] = []
        self.courses: list[Course] = []
        self.candidate_lists = {"names": [], "categories": [], "depts": []}
        self.watcher: Optional[MonitorScheduler] = None
        self.snapshot = SnapshotStore()
        # 风控摘要的唯一数据源：RiskHistory（data/risk_history.json）真实历史。
        # 不再从最近成功课程快照 last_round.json 读——那会在风控命中而本轮无课程时
        # 显示旧值（另一处 source of truth，早晚漂移）。
        self.risk = RiskHistory(path=RISK_FILE)
        # 状态 bundle（归并原先散落的 self.*）
        self.main = MainViewState()          # 主页：view / index / top / snapshot
        self.prog = ProgressState()          # 抓取进度
        self.fv = FilterViewState()          # 筛选页
        self.sv = SettingsViewState()        # 设置页行索引
        self.editing = EditingState()        # 行内编辑态
        # 设置页 draft 与行
        self.sd: dict[str, str] = {}
        self.s_rows: list[tuple[str, dict]] = []
        self.editor = FieldEditor()
        self._detail_course: Optional[Course] = None
        self._f_backup = None
        self._live_timer: Optional[object] = None   # 底部实时刷新的自调度 timer
        # gws 授权状态（首发页展示）：None=尚未探测完(检查中)；对象于后台线程探测后写入
        self._gws_status: Optional[object] = None
        self._gws_auth_probing = False
        self._init_rows()
        self._load_snapshot()

    # ------------------------------------------------------------------
    # 数据 / 字段
    # ------------------------------------------------------------------
    def _init_rows(self) -> None:
        for _g, fields in SETTINGS_FIELDS:
            for f in fields:
                self.s_rows.append((_g, f))
        # 默认 draft 值（打开设置页时刷新）
        self.sd = {f["key"]: "" for _g, f in self.s_rows}

    def _load_snapshot(self) -> None:
        self.courses = self.snapshot.load_courses()
        self.candidate_lists = self.snapshot.candidates()
        ts, meta = self.snapshot.meta()
        self.main.snapshot_ts = ts
        self.main.snapshot_meta = meta
        self.main.risk_summary = self.risk_summary_from_history()

    def risk_summary_from_history(self) -> Optional[tuple]:
        """从真实风控历史加载摘要：(percent, label, hits, total) 或 None。"""
        percent, hits, total = self.risk.evaluate()
        if percent is None:
            return None
        return (percent, risk_label(percent), hits, total)

    # ------------------------------------------------------------------
    # 日志（环形缓冲；落盘由 scheduler/runner 负责）
    # ------------------------------------------------------------------
    def log_line(self, msg: str) -> None:
        line = f"{time.strftime('%H:%M:%S')}  {msg}"
        self.log_buf.append(line)
        if len(self.log_buf) > 1200:
            self.log_buf = self.log_buf[-1200:]
        self._render_log()
        if self.page == "main":
            self._render_main_lite()

    def _thread_log(self, msg: str) -> None:
        if self._shutting_down:
            return
        try:
            self.call_from_thread(self.log_line, msg)
        except RuntimeError:
            # 退出后事件循环已关闭；后台线程只需自行收尾，不能再等待 UI。
            pass

    # ------------------------------------------------------------------
    # 状态文本（数据与排版分离：先取语义数据，再套统一原语）
    # ------------------------------------------------------------------
    @staticmethod
    def _error_summary(detail: str) -> str:
        """把异常现场压缩成主页可读的错误类型；原文只在日志页保留。"""
        text = _plain_markup(detail or "")
        if "验证码" in text or "二次验证" in text:
            return "需要验证码"
        if "登录" in text or "账号登录" in text:
            return "登录失败"
        if "邮件" in text or "gws" in text.lower():
            return "邮件发送失败"
        if "设置" in text and ("失败" in text or "错误" in text):
            return "设置保存失败"
        if "翻页" in text:
            return "翻页失败"
        if "提取" in text or "抓取" in text:
            return "课程数据抓取失败"
        if "风控" in text or "警告" in text:
            return "风控提示"
        if "异常" in text:
            return "本轮异常"
        if "失败" in text or "未成功" in text:
            return "本轮抓取失败"
        return "操作失败"

    def _notify_ready_markup(self) -> str:
        """「通知」= 通知功能的发送就绪度（配置层面，不代表已实际发送过）。
        黄=缺前置条件、绿=具备发送条件。"""
        if not notifier.gws_available():
            return ui_warn("gws 未安装")
        if not self.cfg.notify.to:
            return ui_warn("收件邮箱未填写")
        return ui_ok("已配置")

    def _last_send_markup(self) -> str:
        """「上次发信」= 最近一次真实发信结果（稳定事实，不进底部状态行）。
        未发过→一条连续横线（灰，中性）/ 成功（绿）/ 失败（红）。
        注意：它表示'上次发信'，不是 Gmail 登录状态；授权状态在首发页/设置页呈现。"""
        ok, _ts = notifier.last_mail_status()
        if ok is None:
            return ui_meta("──")
        return ui_ok("成功") if ok else ui_error("失败")

    # ------------------------------------------------------------------
    # 课程行 / 自适应列
    # ------------------------------------------------------------------
    def _visible_rows(self) -> list[Course]:
        fs = FilterSet(self.cfg.filters)
        rows = self.courses
        if self.main.view == "matched":
            rows = rows if self.cfg.filters.empty else [c for c in rows if fs.matches(c)]
        elif self.main.view == "seats":
            rows = [c for c in rows if c.has_seats
                    and (self.cfg.filters.empty or fs.matches(c))]
        if self.main.search_col:
            q = self.main.search_query.strip().lower()
            if q:
                rows = [c for c in rows
                        if q in self._row_search_text(c, self.main.search_col).lower()]
        return rows

    # -- 主页按列查找（1/2/3 三个视图通用，不分工况）---------------------
    def _search_cols(self) -> list[tuple[str, str]]:
        """查找只能在可检索列进行：课程号 / 课程名 / 课程类别 / 开课院系。"""
        return [("no", "课程号"), ("name", "课程名"),
                ("cat", "课程类别"), ("dept", "开课院系")]

    def _search_col_label(self) -> str:
        for k, l in self._search_cols():
            if k == self.main.search_col:
                return l
        return self.main.search_col or ""

    def _begin_search(self) -> None:
        """启动查找：默认按课程名列，弹出编辑器；live filter。

        没有『编辑/浏览』区分：只要没按 Esc 就保持编辑态，关键词即编辑缓冲，
        每次内容变化同步到 main.search_query（canonical）；Enter 直接打开详情。
        """
        if self.main.search_col is None:
            self.main.search_col = "name"
        self.editor.begin(self.main.search_query, "text")
        self.editing.context = "search"
        self.editing.key = "query"
        self._render_main(force=True)

    def _search_cycle_col(self, step: int = 1) -> None:
        cols = self._search_cols()
        if not cols:
            return
        if self.main.search_col is None:
            self.main.search_col = cols[0][0]
        idx = next((i for i, (k, _l) in enumerate(cols)
                    if k == self.main.search_col), 0)
        nxt = (idx + step) % len(cols)
        self.main.search_col = cols[nxt][0]
        self.log_line(f"查找列：{cols[nxt][1]}")
        self._render_main(force=True)

    def _clear_search(self) -> None:
        self.main.search_col = None
        self.main.search_query = ""

    def _leave_search(self) -> None:
        """彻底退出搜索，恢复普通主页（搜索中按 Esc 触发）。"""
        self._clear_search()
        if self.editing.context == "search":
            self._clear_editing()
        else:
            self.editor.reset()
        self._render_main(force=True)

    def _main_hint(self) -> str:
        """主页底部只显示当前状态下真正可用的操作。搜索是持续 live filter：
        编辑态常开，Enter 直接打开详情；宽屏用完整关键词，窄屏自动压缩。"""
        wide = self.size.width >= 115
        if self.main.search_col:
            if wide:
                return _hint(
                    ("←→", "编辑关键词"), ("↑↓ / PgUp / PgDn", "浏览结果"),
                    ("[ ]", "跳转页数"), ("Tab", "换列"),
                    ("Enter", "查看详情"), ("Esc", "退出查找"))
            return _hint(
                ("←→", "编辑关键词"), ("↑↓/PgUp/PgDn", "浏览"),
                ("[ ]", "跳页"), ("Tab", "换列"),
                ("Enter", "详情"), ("Esc", "退出"))
        # 基础操作：没有可展示的数据时，不提示需要数据才能用的操作（如查找/视图）
        parts: list[tuple[str, str]] = [("空格", "开始/停止"), ("r", "立即抓取")]
        if self.courses:
            parts += [("↑↓", "选择课程"), ("[ ]", "跳转页数"), ("/", "按列查找")]
        parts += [("f", "筛选"), ("s", "设置"), ("l", "日志"), ("h", "帮助"), ("q", "退出")]
        return _hint(*parts)

    def _render_search_line(self) -> None:
        """查找区：标题、查找列、输入值各占明确层级。"""
        si = self.query_one("#searchinput", Static)
        if self.main.search_col is None:
            si.display = False
            si.update("")
            return
        si.display = True
        label = ui_key(self._search_col_label())
        value = self.editor.markup()
        si.update("\n".join([
            ui_section("查找"),
            _kv_row("列名", label, width=6),
            _kv_row("输入", value, width=6),
        ]))

    def _row_search_text(self, c: Course, key: str) -> str:
        """某列在列表中的显示文本（查找匹配用，与渲染一致）。"""
        if key == "page":
            return str(c.page) if c.page else "—"
        if key == "no":
            return c.course_no or ""
        if key == "name":
            return c.name or ""
        if key == "cat":
            return c.category or ""
        if key == "dept":
            return c.dept or ""
        if key == "teacher":
            return c.teacher or ""
        if key == "quota":
            return str(c.quota) if c.quota is not None else "—"
        if key == "selected":
            return str(c.selected) if c.selected is not None else "—"
        if key == "avail":
            return str(c.avail) if c.avail >= 0 else "—"
        return ""

    # 结构列/文本列：全部来自 canonical COURSE_COLUMNS，不手写第二份定义。
    # 结构列（数值/ID）固定宽绝不折行；文本列按权重弹性分宽。
    _STRUCT_COLUMNS = [c for c in COURSE_COLUMNS if c.is_structural]
    _TEXT_COLUMNS = [c for c in COURSE_COLUMNS if not c.is_structural]

    def _columns(self, W: int) -> list[tuple[str, str, int]]:
        """按可用宽 W 计算每列目标宽：结构列固定，文本列分剩余宽度。

        列定义来自 canonical schema；这是 TUI 终端适配层的布局决定
        （把文本列塞中间，数值列收尾），不另写第二份列定义。
        """
        structs = self._STRUCT_COLUMNS
        text = self._TEXT_COLUMNS
        GUT = 3
        ncols = len(structs) + len(text)
        fixed = GUT + (ncols - 1) + sum(c.width for c in structs)
        avail = W - fixed               # 分给文本列的总额
        mins = sum(c.min_width for c in text)
        tw = sum(c.weight for c in text)
        if avail < mins:
            widths = [max(2, int(avail * c.weight / tw)) for c in text]
        else:
            widths = [min(c.preferred, c.min_width + int((avail - mins) * c.weight / tw))
                      for c in text]
        # 余量（含 cap 省下的）全部补回课程名，让表格正好填满 W
        widths[0] += avail - sum(widths)
        widths = [max(1, w) for w in widths]
        return ([(c.key, c.title, c.width) for c in structs[:2]]
                + [(c.key, c.title, widths[i]) for i, c in enumerate(text)]
                + [(c.key, c.title, c.width) for c in structs[2:]])

    def _table_width(self, cols) -> int:
        return 3 + sum(width for _key, _label, width in cols) + len(cols) - 1

    def _col_justify(self, key: str) -> str:
        """对齐：统一来自 canonical schema COLUMN.align。"""
        return _col_justify_for(key)

    def _header_labels(self, cols, indent: int = 0) -> str:
        cells = []
        for key, label, width in cols:
            justify = self._col_justify(key)
            cell = self._align_cell(label, width, justify)
            # 查找列列名高亮（启动查找后始终可见）；其余保持 section 灰
            if self.main.search_col and key == self.main.search_col:
                cells.append(f"[bold {ACCENT}]{cell}[/]")
            else:
                cells.append(ui_section(cell))
        return " " * indent + "   " + " ".join(cells)

    @staticmethod
    def _wrap_text(value: str, width: int) -> list[str]:
        """按 terminal cell 宽度折行；不截断、不添加省略号。"""
        width = max(1, width)
        value = str(value or "—")
        result: list[str] = []
        for paragraph in value.splitlines() or [""]:
            if not paragraph:
                result.append("")
                continue
            current = ""
            for char in paragraph:
                if current and cell_len(current + char) > width:
                    result.append(current)
                    current = ""
                current += char
            result.append(current)
        return result or [""]

    @staticmethod
    def _align_cell(value: str, width: int, justify: str = "left") -> str:
        value = str(value)
        padding = max(0, width - cell_len(value))
        if justify == "center":
            left = padding // 2
            return " " * left + value + " " * (padding - left)
        if justify == "right":
            return " " * padding + value
        return value + " " * padding

    def _course_lines(self, c: Course, matched: bool, cols,
                      cursor: bool, indent: int = 0) -> list[str]:
        """渲染一门课程的物理行；折行时其它列保持垂直对齐。

        单元格文本统一来自 canonical course_display，不手写格式化。
        """
        values = {key: course_display(c, key) for key, _l, _w in cols}
        wrapped: dict[str, list[str]] = {}
        for key, _label, width in cols:
            if _col_no_wrap(key):
                # 结构列（数值/ID）绝不折行，只占一个物理行
                wrapped[key] = [str(values[key])]
                continue
            cell_width = width - 2 if key == "name" and matched else width
            wrapped[key] = self._wrap_text(values[key], cell_width)
        height = max(len(parts) for parts in wrapped.values())
        output = []
        for line_no in range(height):
            cells = []
            for key, _label, width in cols:
                parts = wrapped[key]
                text = parts[line_no] if line_no < len(parts) else ""
                justify = self._col_justify(key)
                if key == "name" and matched:
                    aligned = self._align_cell(text, width - 2, justify)
                    marker = ui_key("★" if line_no == 0 else " ")
                    cells.append(f"{marker} {ui_value(aligned)}")
                else:
                    aligned = self._align_cell(text, width, justify)
                    if key == "avail":
                        styled = ui_ok(aligned) if c.has_seats else ui_meta(aligned)
                    else:
                        styled = ui_value(aligned)
                    cells.append(styled)
            cursor_cell = (ui_key("❯") + "  " if cursor and line_no == 0
                           else "   ")
            # 保留单元格填充，供各列稳定对齐。
            output.append(" " * indent + cursor_cell + " ".join(cells))
        return output

    # ------------------------------------------------------------------
    # compose / 生命周期
    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static("", id="hero", markup=True)
        with Vertical(id="stage"):
            # 主页：Hero 顶部稳定摘要 + 课程视图；实时状态在底部 activity。
            with Vertical(id="page-main"):
                yield Static("", id="coursehead", markup=True)
                yield Static("", id="searchinput", markup=True)
                yield CourseViewport("", id="courselist", markup=True)
            # 筛选（pi 式：顶部输入即筛 + 列表，↑↓ 选，空格/回车 切换）
            with Vertical(id="page-filters"):
                yield Static("", id="filters_hdr", markup=True)
                yield Static("", id="fsearch", markup=True)
                with FocusScroll(id="filtersscroll"):
                    yield Static("", id="filters_list", markup=True)
            # 设置（行内编辑，无独立输入框）
            with Vertical(id="page-settings"):
                yield Static("", id="settings_hdr", markup=True)
                with FocusScroll(id="settingsscroll"):
                    yield Static("", id="settings_list", markup=True)
            # 日志
            with Vertical(id="page-logs"):
                with FocusScroll(id="logscroll"):
                    yield Static("", id="logbody", markup=True)
            # 帮助
            with Vertical(id="page-help"):
                with FocusScroll(id="helpscroll"):
                    yield Static("", id="helpbody", markup=True)
            # 课程详情
            with Vertical(id="page-detail"):
                with FocusScroll(id="detscroll"):
                    yield Static("", id="detbody", markup=True)
            # 首次设置（行内编辑邮箱，复用 FieldEditor）
            with Vertical(id="page-setup"):
                yield Static("", id="setupbody", markup=True)
        yield Static("", id="keys")
        yield Static("", id="activity")

    def on_mount(self) -> None:
        self.watcher = MonitorScheduler(self.cfg, log=self._thread_log,
                                        on_round=self._on_round,
                                        on_progress=self._thread_progress)
        # 无周期重绘：底部 live activity 只在真有事件（轮次变化/开始/完成）时
        # 重绘，展示的是绝对时刻而非每秒跳变的计时——避免刷新干扰终端原生 mouse
        # selection（见 tests/tui/test_terminal_selection.py）。
        self._show("main")
        if not self.cfg.first_run_done:
            self.log_line("首次使用：请先完成收件邮箱与 gws 授权设置")
            self._show("setup")

    def on_resize(self, event: events.Resize) -> None:
        if not self.is_mounted:
            return
        self._render_current()

    # ------------------------------------------------------------------
    # 页面切换
    # ------------------------------------------------------------------
    HINTS = {
        # 主页提示为动态（随是否有数据、查找态变化），见 _main_hint()；
        # 这里只保留二级页面与“查找态”之外的常量提示。
        "filters": _page_hint("搜索或选择筛选条件"),
        "settings": _page_hint("↑↓ 选择 · 回车 编辑/执行 · ←→ 切换 · Ctrl+S 保存 · Esc 放弃"),
        "logs": _page_hint("↑↓ / PgUp / PgDn 滚动 · Esc 返回"),
        "help": _page_hint(),
        "detail": _page_hint("Esc 返回"),
    }

    def _page_ids(self):
        return ["main", "filters", "settings", "logs", "help", "detail", "setup"]

    def _anchor_focus(self) -> None:
        """无输入态键盘交给 App 自己接管；不依赖视觉 header 作焦点锚。"""
        self.set_focus(None)

    def _show(self, page: str, *_a, **_k) -> None:
        self.page = page
        for pid in self._page_ids():
            self.query_one(f"#page-{pid}", Vertical).display = (pid == page)
        if page == "filters":
            self._begin_filters()
            self._anchor_focus()
        elif page == "settings":
            self._open_settings()
            self._anchor_focus()
        elif page == "logs":
            self._render_log()
            self.query_one("#logscroll", FocusScroll).focus()
        elif page == "help":
            self._render_help()
            self.query_one("#helpscroll", FocusScroll).focus()
        elif page == "detail":
            self._render_detail()
            self._anchor_focus()
        elif page == "setup":
            self._render_setup()
            self._start_gws_auth_probe()
            self._anchor_focus()
        else:
            self._anchor_focus()
            self._render_main(force=True)
        keys = self.query_one("#keys", Static)
        keys.display = page in _KEYS_PAGES
        if page == "main":
            keys.update(self._main_hint())
        elif page == "setup":
            keys.update(self._setup_hint())
        elif page == "settings":
            keys.update(self._settings_hint())
        elif page == "filters":
            keys.update(self._filters_hint())
        elif page in _KEYS_PAGES:
            keys.update(self.HINTS.get(page, ""))
        self.query_one("#activity", Static).display = page in _ACTIVITY_PAGES
        self._render_runstate()

    def _render_current(self) -> None:
        p = self.page
        if p == "main":
            self._render_main(force=True)
        elif p == "filters":
            self._render_filters_list()
        elif p == "settings":
            self._render_settings_list()
        elif p == "logs":
            self._render_log()
        elif p == "help":
            pass
        elif p == "detail":
            self._render_detail()
        elif p == "setup":
            self._render_setup()
        self._render_runstate()

    # ------------------------------------------------------------------
    # 渲染：主页
    # ------------------------------------------------------------------
    def _clear_editing(self) -> None:
        self.editor.reset()
        self.editing.context = None
        self.editing.key = None
        # 焦点还给惰性锚，避免停在输入态吞键
        self._anchor_focus()

    def _render_main(self, force: bool = False) -> None:
        if not force and self.page != "main":
            return
        self.query_one("#keys", Static).update(self._main_hint())
        self._render_hero()
        self._render_coursehead()
        self._render_search_line()
        self.query_one("#keys", Static).update(self._main_hint())
        # 课程区唯一可靠性：等 Textual 完成布局（搜索开合/首行高度可能刚变）再按
        # #courselist 的真实 content_region 填充满。绝不在 Python 里另算“可用行数”。
        self._schedule_course_render()

    # -- Hero：产品身份 + 稳定摘要（Rich Panel + Table.grid）-------------
    VIEWS = [("all", "1 全部"), ("matched", "2 符合筛选"), ("seats", "3 有空余")]

    def _filter_summary(self) -> str:
        fs = self.cfg.filters
        if fs.empty:
            return ui_meta("未配置（不会告警）")
        group_txt = SEP.join(
            f"{ui_value(n)}×{len(v)}" for n, v in fs.active_groups)
        mode = "任一" if fs.match != "all" else "全部"
        return f"{group_txt}  {ui_meta('满足' + mode + '条件')}"

    def _last_success(self) -> tuple[str, str]:
        """(最近抓取时间, 数据规模)——稳定的摘要，不随 live activity 漂移。"""
        ts = self.main.snapshot_ts or ""
        t = ""
        try:
            t = _compact_dt(datetime.strptime(ts, "%Y-%m-%d %H:%M:%S"))
        except Exception:
            t = ""
        return (t, self.main.snapshot_meta or "")

    def _hero_kv_grid(self, rows) -> Table:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(width=8, justify="left", no_wrap=True, style="dim")
        grid.add_column(no_wrap=True)
        for label, value in rows:
            grid.add_row(label, value)
        return grid

    def _risk_value_markup(self) -> str:
        """风控一行：无样本/未知 → '—'；格式 `percent%（hits/total）`，
        小样本如实显示（如 33%（1/3））；颜色：0/低 绿 · 中 黄 · 高/极高 红。"""
        risk = self.main.risk_summary
        if not risk:
            return ui_meta("—")
        try:
            percent, label, hits, total = risk
            percent, hits, total = int(percent or 0), int(hits or 0), int(total or 0)
        except Exception:
            return ui_meta("—")
        if total <= 0:
            return ui_meta("—")
        if percent <= 0:
            return f"[green]0%（0/{total}）[/]"
        color = {"低": "green", "中": "yellow"}.get(label, "red")
        return f"[{color}]{percent}%（{hits}/{total}）[/]"

    def _render_hero(self) -> None:
        t, meta = self._last_success()
        left_rows = [
            ("筛选", self._filter_summary()),
            ("通知", self._notify_ready_markup()),
            ("上次发信", self._last_send_markup()),
        ]
        right_rows = [
            ("最近抓取", ui_value(t) if t else ui_meta("—")),
            ("数据", ui_value(meta) if meta else ui_meta("—")),
            ("风控", self._risk_value_markup()),
        ]
        left = self._hero_kv_grid(left_rows)
        right = self._hero_kv_grid(right_rows)

        # 两栏：左侧 3 行 > 右侧 2 行，分隔竖线按左列行数画到底（含 上次发信 行）
        two = Table.grid(expand=True)
        two.add_column(ratio=1)
        two.add_column(width=_HERO_SEP)   # 左右两栏之间的分隔区：竖线 + 间距
        two.add_column(ratio=1)
        sep = Text("\n".join("│" for _ in range(len(left_rows))),
                   style=f"dim {ACCENT}")
        two.add_row(left, sep, right)

        # 单栏：窄屏统一堆叠
        one = Table.grid(expand=True)
        one.add_column(ratio=1)
        one.add_row(left)
        one.add_row(right)

        # 是否放得下两栏：两栏平分 (inner-_HERO_SEP)/2（分隔区占 _HERO_SEP），
        # 每栏须装下各自最宽行的自然宽度（按 Rich 实测含 CJK 宽与内边距）。
        # 太窄（<96）一律单栏，避免窄屏退化成两栏挤压。
        two_col = self.size.width >= 96
        if two_col:
            try:
                inner = self.size.width - 4      # 面板边框 2 + 内容内边距 2
                col = (inner - _HERO_SEP) // 2
                two_col = col >= max(_console_measure(left),
                                     _console_measure(right))
            except Exception:
                two_col = True
        body = two if two_col else one
        title = Text()
        title.append(" courser ", style=f"bold {ACCENT}")
        title.append("— PKU 补退选空余名额监控 ", style="dim")
        panel = Panel(body, title=title, title_align="left",
                      border_style=ACCENT, padding=(0, 1), expand=True)
        try:
            self.query_one("#hero", Static).update(panel)
        except Exception:
            pass

    def _render_coursehead(self) -> None:
        head = Text()
        head.append("课程      ", style="bold")
        for key, label in self.VIEWS:
            head.append("   ")
            if self.main.view == key:
                head.append(label, style=f"bold {ACCENT}")
            else:
                head.append(label, style="dim")
        self.query_one("#coursehead", Static).update(head)

    def _render_main_lite(self) -> None:
        """主页的轻量刷新（只刷底部全局活动状态行），不做整页重排。"""
        if self.page != "main":
            return
        self._render_runstate()

    def _course_avail(self) -> int:
        """课程区真实可用行数（唯一 viewport 来源：widget geometry）。"""
        try:
            return max(1, self.query_one("#courselist", Static).content_region.height)
        except Exception:
            return max(1, self.size.height - 19)  # 兜底（未挂载时）

    def _schedule_course_render(self) -> None:
        """等 Textual 完成一次布局后再填课程区；搜索开合/首行高度变化后必需。"""
        try:
            self.call_after_refresh(self._render_courses_from_layout)
        except Exception:
            self._render_courses_from_layout()

    def _render_courses_from_layout(self) -> None:
        if self.page != "main":
            return
        try:
            self._render_course_window(self._visible_rows())
        except Exception:
            pass

    def _render_course_window(self, rows: list[Course]) -> None:
        body = self.query_one("#courselist", Static)
        # fzf 式结果集语义：仅当「可见结果集本身」变化时才把光标回到顶部。
        # 签名 = 结果行身份序列（seq/course_no/name）；两次查询得到完全相同的结果
        # 集时（如「近代」→「近代物」）签名不变 → 留住光标，不打断位置。空结果也
        # 在此复位，下次有结果时从顶部开始。
        sig = tuple((c.seq, c.course_no, c.name) for c in rows)
        if sig != self.main.result_sig:
            self.main.result_sig = sig
            self.main.reset_cursor()
        if not rows:
            # 抓取已经开始后，进度区已经说明当前发生了什么；列表区保持安静。
            if self.prog.done is not None:
                body.update("")
            elif self.main.search_col:
                body.update(
                    "[dim]查找没有结果：修改查找词，或按 Esc 退出查找。[/]")
            elif not self.courses:
                body.update(
                    "[dim]还没有可显示的结果。按 r 立即抓取，或按空格开始监控。[/]")
            else:
                body.update(
                    "[dim]（当前视图无课程：按 1 查看全部 / 按 2 查看符合筛选的课程）[/]")
            return
        avail = max(1, body.content_region.height)
        # `used_lines` 已经把表头算进去了，因此预算就是整个 viewport；
        # 再减一会让课程区永远留出一行空白。
        max_lines = avail
        W = max(40, body.content_region.width or 40)
        cols = self._columns(W)          # 所有列始终存在，按 wrap 布局
        fs = FilterSet(self.cfg.filters)
        if self.main.index >= len(rows):
            self.main.index = len(rows) - 1
        if self.main.index < 0:
            self.main.index = 0

        # 折行模型：一门课可占 1~N 个物理行。按真实物理行分页，保证光标课可见。
        def clines(idx: int, cursor: bool) -> list[str]:
            return self._course_lines(rows[idx], fs.matches(rows[idx]),
                                      cols, cursor=cursor)

        # 让光标课程落入可见窗口：从 top 起累计物理行超 viewport 就上移 top
        top = max(0, min(self.main.top, self.main.index))
        guard = 0
        while guard <= len(rows):
            guard += 1
            used = 1  # 表头
            i = top
            hit = False
            while i < len(rows) and used < max_lines:
                used += len(clines(i, cursor=(i == self.main.index)))
                if i == self.main.index:
                    hit = True
                    break
                i += 1
            if hit or i >= len(rows):
                break
            top += 1
        self.main.top = top

        out = [self._header_labels(cols)]
        used_lines = 1
        i = self.main.top
        while i < len(rows) and used_lines < max_lines:
            room = max_lines - used_lines
            take = clines(i, cursor=(i == self.main.index))[:room]
            if not take:
                break
            out.extend(take)
            used_lines += len(take)
            i += 1
        try:
            body.update("\n".join(out))
        except Exception:
            # 兜底：绝不让任意课程文本触发渲染错误而崩掉 TUI
            body.update("")

    def _build_course_table(self, rows, cols) -> Table:
        """无框 Rich Table：数量/ID 列固定宽，文本列（课程等）吃掉剩余宽度并省略。"""
        fs = FilterSet(self.cfg.filters)
        table = Table(box=None, show_edge=False, show_lines=False,
                      pad_edge=False, padding=(0, 1))
        table.add_column("", justify="center", max_width=1, no_wrap=True)
        for key, label, width in cols:
            justify = self._col_justify(key)
            common = dict(justify=justify, no_wrap=True, overflow="ellipsis")
            # 查找列列名高亮；其余保持普通
            col_label = label
            if self.main.search_col and key == self.main.search_col:
                col_label = Text(label, style=f"bold {ACCENT}")
            if key == "name":
                table.add_column(col_label, ratio=3, **common)
            elif key in ("cat", "dept", "teacher"):
                table.add_column(col_label, ratio=2, **common)
            else:
                table.add_column(col_label, width=width, **common)
        for i, c in enumerate(rows):
            matched = fs.matches(c)
            cells = [Text("❯", style=ACCENT)
                     if (self.main.top + i) == self.main.index else ""]
            for key, _l, _w in cols:
                if key == "name":
                    cells.append(Text(("★ " if matched else "") + (c.name or "—")))
                elif key == "avail":
                    cells.append(Text(course_display(c, key),
                                      style="green" if c.has_seats else "dim"))
                else:
                    cells.append(Text(course_display(c, key)))
            table.add_row(*cells)
        return table

    # ------------------------------------------------------------------
    # 渲染：筛选页
    # ------------------------------------------------------------------
    def _begin_filters(self) -> None:
        self._f_backup = copy.deepcopy(self.cfg.filters)  # Esc 放弃依据
        self.fv.dim = 0
        self.fv.query = ""
        self.fv.focus = "search"
        self.fv.index = 0
        self.fv.top = 0
        self._render_filters_list()
        self._ensure_candidates()

    def _ensure_candidates(self) -> None:
        # 候选来自最近一次抓取结果（candidate_lists）。若一个结果都没有，
        # 自动抓一轮来生成候选，避免用户面对空列表无从选择。
        if not self.fv.auto_gather or self.fv.gathering or self.courses:
            return
        if self.watcher and self.watcher.running:
            return
        self.fv.gathering = True
        self.log_line("暂无最近抓取结果，自动抓取一轮以生成候选…")
        self._render_filters_list()
        self._request_round()

    def _on_gather_done(self) -> None:
        self.fv.gathering = False
        if self.page == "filters":
            self._render_filters_list()

    def _filters_entries(self) -> list[str]:
        return list(getattr(self.cfg.filters, GROUPS[self.fv.dim][0]))

    def _filters_dim_cycle(self) -> None:
        self.fv.dim = (self.fv.dim + 1) % len(GROUPS)
        self.fv.focus = "search"
        self.fv.index = 0
        self.fv.top = 0
        self._render_filters_list()

    def _dim_custom_allowed(self) -> bool:
        """课程类别只能从自动探测的候选中选择，不允许手动添加；其余维度可以。"""
        return GROUPS[self.fv.dim][0] != "categories"

    def _filters_items(self) -> list[str]:
        """当前列表 = 快照候选（按输入即筛的查询过滤）+ 已选但不在候选里的。"""
        gid = GROUPS[self.fv.dim][0]
        q = self.fv.query.lower()
        seen = set(self.candidate_lists[gid])
        items = [e for e in self.candidate_lists[gid] if (not q or q in e.lower())]
        if self._dim_custom_allowed():
            # 仅可手动添加的维度，把已选但不在候选里的条目一并列出
            for e in getattr(self.cfg.filters, gid):
                if e not in seen and (not q or q in e.lower()):
                    items.append(e)
        return items

    def _render_filters_list(self) -> None:
        dim_name = GROUPS[self.fv.dim][1]
        entries = set(self._filters_entries())
        mode = "满足任一条件" if self.cfg.filters.match != "all" else "满足全部条件"
        self.query_one("#filters_hdr", Static).update(ui_title("筛选"))
        # pi 式输入即筛行
        fs = self.query_one("#fsearch", Static)
        if self.fv.gathering:
            fs.update("\n".join([
                _kv_row("维度", ui_key(dim_name)),
                _kv_row("组合", ui_value(mode)),
                _kv_row("已选", ui_value(str(len(entries)))),
                "",
                ui_meta("正在抓取候选…完成后会自动列出课程名 / 课程类别 / 开课院系"),
            ]))
        else:
            q = ui_value(self.fv.query)
            search_marker = ui_key("❯") if self.fv.focus == "search" else " "
            input_cursor = "[cyan]▍[/]" if self.fv.focus == "search" else ""
            fs.update("\n".join([
                _kv_row("维度", ui_key(dim_name)),
                _kv_row("组合", ui_value(mode)),
                _kv_row("已选", ui_value(str(len(entries)))),
                "",
                f"{search_marker} {ui_key('搜索')}  {q}{input_cursor}"
                + (f"  {ui_meta('输入文字可过滤下方列表')}" if not self.fv.query else ""),
            ]))
        items = self._filters_items()
        listw = self.query_one("#filters_list", Static)
        try:
            self.query_one("#keys", Static).update(self._filters_hint())
        except Exception:
            pass
        if not items:
            if self.fv.gathering:
                listw.update(ui_meta("正在抓取候选，列表待生成…"))
                return
            if self.fv.query:
                if self._dim_custom_allowed():
                    message = (f"没有匹配「{ui_value(self.fv.query)}」的条目。\n"
                               "按 Enter 可把它添加为当前维度的筛选条件。")
                else:
                    message = (f"没有匹配「{ui_value(self.fv.query)}」的课程类别。\n"
                               "课程类别只能从自动探测的候选中选择，不能手动添加。")
            else:
                if self._dim_custom_allowed():
                    message = ("本维度暂无可选条目。\n"
                               "候选取自最近一次抓取结果。\n"
                               "还没有结果时，在主页按 r 抓一轮，或输入文字后按 Enter 添加。")
                else:
                    message = ("本维度暂无可选的课程类别。\n"
                               "候选取自最近一次抓取结果（自动探测）。\n"
                               "还没有结果时，在主页按 r 抓一轮。")
            listw.update("\n".join([ui_meta(message), ""]))
            return
        if self.fv.index >= len(items):
            self.fv.index = len(items) - 1
        if self.fv.index < self.fv.top:
            self.fv.top = self.fv.index
        cands = set(self.candidate_lists[GROUPS[self.fv.dim][0]])
        out = []
        for idx, it in enumerate(items):
            cur = "[cyan]❯[/]" if (self.fv.focus == "list"
                                     and idx == self.fv.index) else " "
            mark = "[cyan]✓[/]" if it in entries else " "
            extra = " [dim](手动添加)[/]" if it not in cands else ""
            out.append(f"{cur} {mark} {ui_value(it)}{extra}")
        listw.update("\n".join(out))
        # 搜索框是这条纵向焦点链的起点。重进页面、切维度或改查询时会回到
        # SEARCH/index=0；同时必须清掉上次浏览候选留下的物理 scroll_y，不能
        # 只重置状态而让列表仍停在旧位置，把焦点标记裁到 viewport 外。
        follow = (self._scroll_filters_to_selection if self.fv.focus == "list"
                  else self._reset_filters_scroll)
        self.call_after_refresh(follow)

    def _filters_hint(self) -> str:
        """只显示当前焦点真正可用的键，消除 Space 的二义性。"""
        if self.fv.focus == "list":
            return _hint(
                ("↑↓", "移动"), ("Space", "选中/取消"),
                ("Enter", "保存"), ("Tab", "切换维度"),
                ("↑ 至顶部", "返回搜索"),
            )
        parts = [("文字 / Space", "搜索"), ("↓", "进入结果")]
        if (self.fv.query and not self._filters_items()
                and self._dim_custom_allowed()):
            parts.append(("Enter", "添加当前条件"))
        parts += [("Tab", "切换维度"), ("Esc", "清空/放弃")]
        return _hint(*parts)

    def _scroll_filters_to_selection(self) -> None:
        """让筛选光标保持在真实 viewport 内，且只在越界时最小滚动。"""
        if self.fv.focus != "list":
            return
        items = self._filters_items()
        if not items:
            return
        try:
            scroll = self.query_one("#filtersscroll", FocusScroll)
            body = self.query_one("#filters_list", Static)
        except Exception:
            return
        row = Region(
            0,
            body.virtual_region.y + self.fv.index,
            scroll.scrollable_content_region.width,
            1,
        )
        scroll.scroll_to_region(row, animate=False, force=True)

    def _reset_filters_scroll(self) -> None:
        """SEARCH 焦点时回到候选起点，令状态索引与真实 viewport 一致。"""
        try:
            scroll = self.query_one("#filtersscroll", FocusScroll)
            scroll.scroll_to(y=0, animate=False, force=True, immediate=True)
        except Exception:
            pass

    def _filters_move(self, step: int) -> None:
        items = self._filters_items()
        if not items:
            return
        self.fv.index = min(max(0, self.fv.index + step), len(items) - 1)
        self._render_filters_list()

    def _filters_toggle(self) -> None:
        items = self._filters_items()
        if not (0 <= self.fv.index < len(items)):
            return
        gid = GROUPS[self.fv.dim][0]
        lst = getattr(self.cfg.filters, gid)
        value = items[self.fv.index]
        if value in lst:
            lst.remove(value)
            self.log_line(f"已取消筛选：{value}")
        else:
            lst.append(value)
            self.log_line(f"已添加筛选：{value}")
        self._render_filters_list()

    def _filters_type(self, char: str) -> None:
        self.fv.query += char
        self.fv.index = 0
        self.fv.top = 0
        self._render_filters_list()

    def _filters_backspace(self) -> None:
        if not self.fv.query:
            return
        self.fv.query = self.fv.query[:-1]
        self.fv.index = 0
        self.fv.top = 0
        self._render_filters_list()

    def _leave_filters(self, commit: bool) -> None:
        if commit:
            self.cfg.save()
            self.log_line("筛选已保存")
        else:
            # Esc：放弃本次改动，还原到进入时的状态
            self.cfg.filters = copy.deepcopy(getattr(self, "_f_backup", self.cfg.filters))
            self.log_line("已放弃筛选修改")
        self._show("main", force=True)

    # ------------------------------------------------------------------
    # 渲染：设置页（draft 事务）
    # ------------------------------------------------------------------
    def _enum_label_for(self, f: dict) -> str:
        val = self.sd.get(f["key"], "")
        for value, label in f["opts"]:
            if value == val:
                return label
        return str(val)

    def _draft_display(self, f: dict) -> str:
        if f["kind"] == "password":
            return "•" * 8 if self.sd.get(f["key"]) else "（空）"
        if f["kind"] == "enum":
            return self._enum_label_for(f)
        return self.sd.get(f["key"]) or "（空）"

    def _open_settings(self) -> None:
        # 从持久化刷新 draft；进入后只改 draft，Esc 丢弃
        for _g, f in self.s_rows:
            self.sd[f["key"]] = _field_value(self.cfg, f["key"])
        self.sv.index = 0
        self._render_settings_list()
        self.set_focus(None)
        self.call_after_refresh(self._scroll_settings_to_selection)  # 首次布局未定，延迟一下

    def _render_settings_list(self) -> None:
        hdr = self.query_one("#settings_hdr", Static)
        hdr.update(ui_title("设置"))
        # 顶部不再重复展示「gws ✓ 已安装」——那是 Hero「通知」的职责。
        # Settings 只在邮件分组里给出该环境异常时的局部诊断（正常不报喜）。
        MAIL_GROUP = ("邮件通知（gws 发送，需先 `gws auth setup` 后 `gws auth login` 授权）")
        group_names = {
            "账号凭据（可选；留空则依赖浏览器密码管理器自动填充）":
                ("账号", "可留空，登录时依赖浏览器密码管理器自动填充"),
            MAIL_GROUP: ("邮件", "通过 gws 发送提醒"),
            "轮询节奏（自动带随机抖动）": ("轮询", "自动带随机抖动"),
            "行为": ("浏览器", "会话与窗口行为"),
        }
        out: list[str] = []
        row_y: dict[int, int] = {}
        last = None
        for i, (gname, f) in enumerate(self.s_rows):
            if gname != last:
                title, _note = group_names.get(gname, (gname, ""))
                # 邮件分组：正常只提示「通过 gws 发送提醒」；gws 缺失才用告警色诊断。
                if gname == MAIL_GROUP:
                    if not notifier.gws_available():
                        note_markup = ui_warn(
                            f"  ✗ 未找到 gws，请先执行 {notifier.GWS_INSTALL_COMMAND}，"
                            f"再执行 {notifier.GWS_SETUP_COMMAND} 与 {notifier.GWS_LOGIN_COMMAND}")
                    else:
                        note_markup = ui_meta("  通过 gws 发送提醒")
                elif _note:
                    note_markup = ui_meta(f"  {_note}")
                else:
                    note_markup = ""
                if last is not None:
                    out.append("")
                out.append(ui_section(title))
                if note_markup:
                    out.append(note_markup)
                last = gname
            row_y[i] = len(out)   # 记录该字段行在正文中的起始行号（供自动滚动）
            cur = "[cyan]❯[/]" if i == self.sv.index else " "
            if (i == self.sv.index and self.editing.context == "settings"
                    and self.editing.key == f["key"]):
                out.append(_kv_row(f["label"], self.editor.markup(),
                                   width=16, prefix=f"{cur} "))
                continue
            if f["kind"] == "action":
                # 动作行：发送一封测试邮件（第一次回车请求执行，第二次回车确认发送）。
                # 发送在后台线程进行，UI 立即切到「发送中…」，不阻塞主线程。
                if self.sv.test_mail_sending:
                    value = ui_warn("发送中…")
                elif self.sv.test_mail_result is True:
                    value = ui_ok("发送成功")
                elif self.sv.test_mail_result is False:
                    value = ui_error("发送失败")
                elif self.sv.test_mail_armed_at is not None:
                    value = ui_warn("再次回车确认发送")
                elif notifier.gws_available():
                    value = ui_meta("回车发送")
                else:
                    value = ui_warn("gws 未安装，无法发送")
                out.append(_kv_row(f["label"], value, width=18,
                                   prefix=f"{cur} "))
                continue
            val = self._draft_display(f)
            if val == "（空）":
                value = ui_meta(val)
            elif i == self.sv.index:
                value = ui_key(val)
            else:
                value = ui_value(val)
            out.append(_kv_row(f["label"], value, width=16,
                               prefix=f"{cur} "))
        # 底部操作提示由全局 #keys 固定展示（见 _settings_hint），不再写在正文里。
        self.sv.row_y = row_y
        self.query_one("#settings_list", Static).update("\n".join(out))
        self._scroll_settings_to_selection()
        try:
            self.query_one("#keys", Static).update(self._settings_hint())
        except Exception:
            pass

    def _settings_hint(self) -> str:
        """设置页底部操作栏：只显示当前选中行真正可用的操作。
        字段编辑态切到编辑键位；←→ 切换仅在 enum 行可用，其它行（文本/动作）不提示。"""
        if self.editing.context == "settings":
            return self._EDIT_HINT
        parts: list[tuple[str, str]] = [("↑↓", "选择")]
        _g, f = self.s_rows[self.sv.index]
        if f["kind"] == "enum":
            parts.append(("←→", "切换"))
        parts += [("Enter", "编辑/执行"), ("Ctrl+S", "保存"), ("Esc", "放弃")]
        return _hint(*parts)

    def _setup_hint(self) -> str:
        """首启设置页底部操作栏：非编辑态 Enter=编辑邮箱、Ctrl+S=保存并继续；
        编辑态则走统一的行内编辑键位（_EDIT_HINT）。"""
        if self.editing.context == "setup":
            return self._EDIT_HINT
        return _hint(
            ("Enter", "编辑邮箱"),
            ("Ctrl+S", "保存并继续"),
            ("Esc", "退出程序"),
        )

    def _scroll_settings_to_selection(self) -> None:
        """↑↓ 移动设置项时保持选中行可见，但**不把滚动条钉到顶部**：交给 Textual 的
        scroll_to_region 做最小必要滚动（选中行已在真实 viewport 内则完全不动）。
        不再手工推算 top/bottom——row_y 是 #settings_list 局部行号，先用 virtual_region
        换算成 #settingsscroll 内容坐标，避免坐标系混用导致 off-by-N。"""
        try:
            scroll = self.query_one("#settingsscroll", FocusScroll)
            body = self.query_one("#settings_list", Static)
        except Exception:
            return
        row = Region(
            0,
            body.virtual_region.y + self.sv.row_y.get(self.sv.index, 0),
            scroll.scrollable_content_region.width,
            1,
        )
        scroll.scroll_to_region(row, animate=False, force=True)

    # -- 可复用的行内编辑（settings 与 setup 共用同一个 FieldEditor）-----
    _EDIT_HINT = _hint(
        ("←/→", "移动光标"), ("退格 / Delete", "删除"),
        ("Home / End", "行首 / 行尾"), ("回车", "确认"), ("Esc", "取消"),
    )

    def _begin_field(self, context: str, key: str, kind: str, value: str) -> None:
        """进入任意字段的行内编辑（值保留在该行，光标置于末尾）。"""
        self.editor.begin(value, kind)
        self.editing.context = context
        self.editing.key = key
        self._render_field_page()

    def _render_field_page(self) -> None:
        if self.page == "main":
            self._render_main(force=True)
        elif self.page == "settings":
            self._render_settings_list()
        elif self.page == "setup":
            self._render_setup()

    def _edit_key(self, event: events.Key) -> None:
        """编辑态按键统一走 FieldEditor；commit/cancel 才由调用方落盘。

        搜索态在 on_key 已拦截 Esc/↑↓/[/]/Tab/Enter，这里只做纯文本编辑
        （←→ 移光标、字符/退格即改）；每次变化即时同步到 main.search_query。"""
        # Textual exposes the actual typed character separately from the key
        # name.  A symbol such as ``@`` may have a named key (``at``), so
        # using only event.key silently drops it.
        character = event.character
        out = self.editor.feed(event.key,
                               character,
                               event.is_printable)
        event.stop()
        if out == "commit":
            val = self.editor.text
            context, key = self.editing.context, self.editing.key
            self._clear_editing()
            if context == "settings" and key:
                self.sd[key] = val
            elif context == "setup":
                self.cfg.notify.to = val
                self.log_line("收件邮箱已更新")
            self._render_field_page()
        elif out == "cancel":
            self._clear_editing()
            self._render_field_page()
        else:
            if self.editing.context == "search":
                self.main.search_query = self.editor.text
            self._render_field_page()   # 移动光标 / 删除 / 插入后刷新（查找=输入即筛）

    def _settings_edit(self, f: dict) -> None:
        # 进入该字段的行内编辑（复用 FieldEditor）
        self._begin_field("settings", f["key"], f["kind"],
                          self.sd.get(f["key"], ""))

    def _settings_move(self, step: int) -> None:
        n = len(self.s_rows)
        self.sv.index = min(max(0, self.sv.index + step), n - 1)
        self._render_settings_list()

    def _settings_cycle(self, f: dict, step: int) -> None:
        opts = f["opts"]
        cur = self.sd.get(f["key"])
        idx = next((i for i, (v, _l) in enumerate(opts) if v == cur), 0)
        nxt = (idx + step) % len(opts)
        self.sd[f["key"]] = opts[nxt][0]
        self.log_line(f"{f['label']} → {opts[nxt][1]}（待保存）")
        self._render_settings_list()

    def _settings_save(self) -> None:
        # 先整体校验，再一次性写回（避免部分字段被改坏）
        numeric = {"int": lambda v: int(float(v)),
                   "float": lambda v: float(v)}
        try:
            for _g, f in self.s_rows:
                if f["kind"] in numeric:
                    numeric[f["kind"]](self.sd.get(f["key"], ""))
        except ValueError:
            self.log_line("✗ 有数字字段格式不正确，未保存")
            return
        try:
            for _g, f in self.s_rows:
                if f["kind"] == "action":
                    continue   # 动作行（发送测试邮件）不是可保存字段
                _field_mutate(self.cfg, f["key"], self.sd.get(f["key"], ""))
            self.cfg.save()
        except ValueError:
            self.log_line("✗ 保存失败：数值越界/格式错误，未写入")
            return
        self.log_line("设置已保存")
        self._leave_settings(commit=True)

    def _leave_settings(self, commit: bool) -> None:
        self._clear_editing()
        self.sv.test_mail_armed_at = None   # 离开设置页 = 放弃/结束确认窗口
        self.sv.test_mail_result = None     # 清掉测试发送结果（后台发送是否仍在跑由 worker 自主收尾）
        if not commit:
            self.log_line("已放弃设置修改")
        self._show("main", force=True)

    # ------------------------------------------------------------------
    # 渲染：日志页 / 帮助 / 详情 / 首启设置
    # ------------------------------------------------------------------
    def _render_log(self) -> None:
        body = self.query_one("#logbody", Static)
        lines = [ui_title("运行日志"), ""]
        if not self.log_buf:
            lines.extend([ui_meta("暂无日志；开始监控或抓取后这里会记录每一轮过程"), ""])
            body.update("\n".join(lines))
            return
        for raw in self.log_buf[-300:]:
            plain = _plain_markup(raw)
            if "  " in plain:
                timestamp, message = plain.split("  ", 1)
            else:
                timestamp, message = "", plain
            if "✗" in message or "失败" in message or "异常" in message:
                styled = ui_error(message)
            elif "⚠" in message or "风控" in message:
                styled = ui_warn(message)
            else:
                styled = ui_value(message)
            lines.append((f"{ui_meta(timestamp)}  " if timestamp else "") + styled)
        body.update("\n".join(lines))

    def _render_help(self) -> None:
        lines = [ui_title("帮助"), "", ui_section("鼠标"),
                 _shortcut_row("拖动", "选择文字，松手自动复制"),
                 _shortcut_row("滚轮", "滚动页面（不改光标）"), "",
                 ui_section("主页"),
                 _shortcut_row("Space", "开始 / 停止监控"),
                 _shortcut_row("r", "立即抓取一轮"),
                 _shortcut_row("1", "全部课程"),
                 _shortcut_row("2", "符合筛选"),
                 _shortcut_row("3", "有空余名额"),
                 _shortcut_row("/", "按列查找（输入即筛，Tab 换列，←→ 跳转结果页数）"),
                 _shortcut_row("Esc", "清除查找"),
                 _shortcut_row("↑ / ↓", "选择课程"),
                 _shortcut_row("Enter", "查看详情"), "",
                 _shortcut_row("f", "筛选"), _shortcut_row("s", "设置"),
                 _shortcut_row("l", "日志"), _shortcut_row("h", "帮助"),
                 _shortcut_row("q", "退出"), "",
                 ui_section("筛选"),
                 _shortcut_row("Tab", "切换课程名 / 类别 / 开课院系"),
                 _shortcut_row("↑ / ↓", "移动"),
                 _shortcut_row("Space", "选中 / 取消"),
                 _shortcut_row("Enter", "保存并返回"),
                 _shortcut_row("Esc", "放弃修改"), "",
                 ui_section("设置"),
                 _shortcut_row("↑ / ↓", "选择字段"),
                 _shortcut_row("← / →", "切换选项或移动光标"),
                 _shortcut_row("Enter", "编辑"),
                 _shortcut_row("Enter ×2", "发送测试邮件（2 秒内确认）"),
                 _shortcut_row("Ctrl+S", "保存"),
                 _shortcut_row("Esc", "放弃修改"), "",
                 ui_section("监控流程"),
                 "  检查/复用已有会话", "    → 失效时 IAAA 登录", "    → 补退选",
                 "    → 动态读取所有页面", "    → 筛选空余课程",
                 "    → 邮件通知", "",
                 ui_meta("默认复用有效会话；设置中开启“每轮强制重新登录”才会先登出。"),
                 "", ui_section("安全与节奏"),
                 "  轮询带随机抖动；发现风控或警告语会放慢节奏并提示。",
                 "  登录失败、验证码等错误会自动降速，不硬顶。",
                 "", _page_hint()]
        self.query_one("#helpbody", Static).update("\n".join(lines))

    def _render_detail(self) -> None:
        c = self._detail_course
        if c is None:
            self._show("main", force=True)
            return
        lines = [ui_title("课程详情"), "", ui_section(c.name or "（无名称）"), ""]
        avail = "—" if c.avail < 0 else str(c.avail)
        seat_color = "green" if c.has_seats else "dim"
        fields = {attr: getattr(c, attr, "") for _label, attr in COURSE_DETAIL_FIELDS}
        lines.append(ui_section("基本信息"))
        for label, attr in (("课程号", "course_no"), ("课程类别", "category"),
                            ("开课单位", "dept"), ("教师", "teacher"),
                            ("课序号", "class_no"), ("学分", "credits"),
                            ("年级", "grade")):
            lines.append(_kv_row(label, ui_value(str(fields.get(attr) or "—")), width=12))
        lines.extend(["", ui_section("名额"),
                      _kv_row("限数 / 已选", ui_value(str(fields.get("seats_raw") or "—")), width=12),
                      _kv_row("空余", f"[{seat_color}]{avail}[/]", width=12),
                      _kv_row("选课状态", ui_value(str(fields.get("status") or "—")), width=12),
                      "", ui_section("时间"),
                      _kv_row("时间地点", ui_value(str(fields.get("schedule") or "—")), width=12),
                      _kv_row("周学时", ui_value(str(fields.get("weekly_hours") or "—")), width=12),
                      _kv_row("P / NP", ui_value(str(fields.get("pnp") or "—")), width=12),
                      "", ui_section("抓取信息"),
                      _kv_row("所属页数", ui_value(str(fields.get("page") or "—")), width=12),
                      _kv_row("课程 ID", ui_value(str(fields.get("seq") or "—")), width=12),
                      "", ui_meta("★ 表示符合当前筛选"), "", _page_hint()])
        self.query_one("#detbody", Static).update("\n".join(lines))

    def _render_setup(self) -> None:
        body = self.query_one("#setupbody", Static)
        gws = notifier.gws_available()
        has_recip = bool(self.cfg.notify.to)
        lines = [ui_title("首次设置"), "", ui_section("运行环境")]
        import shutil
        opencli = shutil.which("opencli")
        lines.append("  " + ("[green]✓[/] opencli 已安装"
                             if opencli else "[red]✗[/] opencli 未安装（请先安装并运行 opencli doctor）"))
        lines.append("  " + ("[green]✓[/] gws 已安装"
                             if gws else f"[red]✗[/] gws 未安装（{notifier.GWS_INSTALL_COMMAND}）"))
        if gws:
            st = self._gws_status  # None = 尚未探测完（显示检查中）
            if st is None:
                lines.append("  [cyan]…[/] 正在检查 gws 授权状态…")
            elif st.auth == "ready":
                acc = f"（{st.account}）" if st.account else ""
                lines.append(f"  [green]✓[/] gws 已授权{acc}")
            elif st.auth == "invalid":
                lines.append(f"  [yellow]⚠[/] gws token 已失效：请执行 {notifier.GWS_LOGIN_COMMAND}")
            elif st.auth == "missing":
                lines.append(f"  [yellow]⚠[/] gws 尚未授权：请执行 {notifier.GWS_SETUP_COMMAND}（一次性），"
                             f"再执行 {notifier.GWS_LOGIN_COMMAND}")
            else:
                lines.append("  [yellow]⚠[/] 无法检测 gws 授权状态（请执行 gws auth status 查看）")
        lines.extend(["", ui_section("收件邮箱"), ui_meta("用于接收提醒，必填")])
        if self.editing.context == "setup" and self.editing.key == "to":
            lines.append(_kv_row("邮箱", self.editor.markup(), width=12, prefix="❯ "))
        else:
            if has_recip:
                lines.append(_kv_row("邮箱", ui_value(self.cfg.notify.to), width=12, prefix="❯ "))
            else:
                lines.append(_kv_row("邮箱", ui_warn("未填写"), width=12, prefix="❯ "))
        lines.append("")
        lines.append(ui_meta("学号 / 密码可留空：登录时由浏览器密码管理器自动填充。"))
        lines.append(ui_meta("也可稍后在主页按 s，在「设置 → 账号」中补充。"))
        lines.append("")
        if has_recip:
            lines.append(ui_ok("收件邮箱已配置，可以开始使用。"))
        else:
            lines.append(ui_warn("请先填写收件邮箱。"))
        body.update("\n".join(lines))
        # 底部操作栏与编辑态同步（编辑=_EDIT_HINT，否则=_setup_hint）
        try:
            self.query_one("#keys", Static).update(self._setup_hint())
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 首启设置：收件邮箱同样用 FieldEditor 行内编辑（与设置页同一套代码）
    # ------------------------------------------------------------------
    def _setup_edit_email(self) -> None:
        self._begin_field("setup", "to", "text", self.cfg.notify.to)

    # -- gws 授权状态：后台线程探测 + 缓存，不阻塞渲染 -------------------
    def _start_gws_auth_probe(self) -> None:
        """异步探测 gws 授权状态（run_in_executor + 缓存）。
        未安装则不探测；同一时刻不重复起线程。"""
        if not notifier.gws_available():
            return
        if self._gws_auth_probing:
            return
        self._gws_status = None          # 置 None 让正文显示“检查中”
        self._gws_auth_probing = True
        try:
            self.run_worker(self._gws_auth_worker(), name="courser-gws-auth")
        except Exception:
            self._gws_auth_probing = False

    async def _gws_auth_worker(self) -> None:
        try:
            self._gws_status = await asyncio.to_thread(notifier.gws_auth_status_cached)
        except Exception:
            self._gws_status = notifier.GwsStatus(auth="unknown")
        finally:
            self._gws_auth_probing = False
        if self.page == "setup":
            try:
                self._render_setup()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 抓取进度（本轮步骤 + 当前操作实时提示）
    # ------------------------------------------------------------------
    def _thread_progress(self, done, total, op):
        if self._shutting_down:
            return
        try:
            self.call_from_thread(self._set_progress, done, total, op)
        except RuntimeError:
            pass

    def _set_progress(self, done, total, op):
        self.prog.done, self.prog.total, self.prog.op = done, total, op
        self._start_live_ticker()
        try:
            self._render_runstate()
        except Exception:
            pass

    def _start_live_ticker(self) -> None:
        """拉起底部实时刷新（自调度 set_timer）：只在抓取/监控进行中每秒重绘，
        空闲即自动停止，避免无谓刷新干扰终端鼠标选区。"""
        if self._live_timer is not None:
            return
        self._live_timer = self.set_timer(1.0, self._live_tick)

    def _live_tick(self) -> None:
        """1s 定时器：仍在抓取/监控中则刷新底部（时间实时走），空闲则不再续期。"""
        self._live_timer = None
        w = self.watcher
        if w and (w.running or w.current_round_started_at is not None):
            try:
                self._render_runstate()
            except Exception:
                pass
            self._live_timer = self.set_timer(1.0, self._live_tick)

    def _next_round_text(self, w) -> str:
        """下一轮倒计时（mm 分 ss 秒），随底部每秒刷新更新。"""
        if not w or not w.next_round_ts:
            return "—"
        remain = max(0, int(w.next_round_ts - time.time()))
        m, s = divmod(remain, 60)
        return f"{m} 分 {s:02d} 秒"

    def _activity_fetching(self, w) -> str:
        """抓取过程中（实时活动）：• 状态 抓取中 · 正在… │ 已用 Ns（实时刷新）。
        不显示步数/页数；已用秒数通过 _live_tick 每秒刷新。"""
        seg = [f"{ui_meta('• 状态')}  {ui_warn('抓取中')}"]
        if self.prog.op:
            seg.append(ui_value(str(self.prog.op)))
        elapsed = time.time() - w.current_round_started_at
        seg.append(ui_meta(f"已用 {elapsed:.0f}s"))
        return " │ ".join(seg)

    def _activity_steady(self, w) -> str:
        """监控等待 / 空闲（实时活动）：• 状态 <上一轮结果> │ 下一轮 <时刻>。
        只报状态，不重复页数/课程数——那是顶部 Hero 的职责。
        状态语义：监控中(绿) / 成功抓取(绿) / 触发风控(红) / 达到重试上限(红) /
        抓取失败(红) / 本轮已停止(黄) / 未开始(灰)。"""
        anchor = ui_meta("• 状态")
        last = w.last_result if w else None
        if w and w.running:
            status = ui_ok("监控中")
        elif last is None:
            status = ui_meta("未开始")
        elif last.cancelled:
            status = ui_warn("本轮已停止")
        elif last.failure_kind == FetchFailureKind.BROWSER_UNAVAILABLE:
            status = ui_error("浏览器不可用")
        elif last.warning_hit:
            status = ui_error("触发风控")
        elif last.retry_exhausted:
            status = ui_error("达到重试上限")
        elif not last.ok:
            status = ui_error("抓取失败")
        else:
            status = ui_ok("成功抓取")
        seg = [f"{anchor}  {status}"]
        nxt = self._next_round_text(w)
        seg.append(f"{ui_meta('下一轮')} "
                   f"{ui_meta('—') if nxt == '—' else ui_value(nxt)}")
        return " │ ".join(seg)

    def _render_runstate(self) -> None:
        """底部唯一的全局 activity 行：抓取中 / 监控等待 / 空闲 — 互斥，只留一行。
        只在真有事件（抓取轮变化、开始/完成）时重绘，杜绝每秒周期刷新。"""
        w = self.watcher
        if self._shutdown_message:
            line = f"{ui_meta('状态')}  {ui_warn(self._shutdown_message)}"
        elif w and w.current_round_started_at is not None:
            line = self._activity_fetching(w)
        else:
            line = self._activity_steady(w)
        self.query_one("#activity", Static).update(line)

    # ------------------------------------------------------------------
    # 键盘路由
    # ------------------------------------------------------------------
    def on_key(self, event: events.Key) -> None:
        key = event.key
        # 查找态（持续 live filter）：Esc 退出搜索；↑↓/PgUp/PgDn 浏览结果；
        # [/] 跳课程页；Tab 换列；Enter 打开当前课程详情；其余交给 FieldEditor
        # （←→ 移光标，字符/退格即改即筛）。仅主页生效——搜索草稿在详情页残留时
        # 按键交给页面级处理（Esc 返回搜索页，而不是退出搜索）。
        if self.editing.context == "search" and self.page == "main":
            if key == "escape":
                event.stop()
                self._leave_search()
            elif key in ("up", "down", "pageup", "pagedown"):
                event.stop()
                self._handle_cursor_nav(key)
            elif key in ("[", "left_square_bracket"):
                event.stop()
                self._move_page_cursor(-1)
            elif key in ("]", "right_square_bracket"):
                event.stop()
                self._move_page_cursor(1)
            elif key == "tab":
                event.stop()
                self._search_cycle_col(1)
            elif key == "enter":
                event.stop()
                self._open_detail()
            else:
                self._edit_key(event)
            return
        if self.editing.context and self.editing.context != "search":
            # 任何行内编辑（settings / setup）统一走 FieldEditor
            self._edit_key(event)
            return
        k = event.key
        page = self.page
        # q = 退出（筛选页里 q 是搜索字符，用 Esc 返回后 q 或 Ctrl+C 退出）
        if page != "filters" and k == "q":
            event.stop()
            self.action_quit()
            return

        if page == "setup":
            self._setup_key(k, event)
            return
        if page == "settings":
            self._settings_key(k, event)
            return
        if page == "filters":
            self._filters_key(k, event)
            return
        if page == "logs" or page == "help" or page == "detail":
            if k == "escape":
                event.stop()
                self._show("main", force=True)
            return
        # main
        if page == "main":
            self._main_key(k, event)

    def _filters_key(self, k: str, event: events.Key) -> None:
        # 搜索框与候选是同一纵向焦点链：↓ 进入列表，列表首项 ↑ 返回搜索。
        if k == "tab":
            event.stop()
            self._filters_dim_cycle()
        elif k == "up":
            event.stop()
            if self.fv.focus == "list":
                if self.fv.index == 0:
                    self.fv.focus = "search"
                    self._render_filters_list()
                else:
                    self._filters_move(-1)
        elif k == "down":
            event.stop()
            if self.fv.focus == "search":
                if self._filters_items():
                    self.fv.focus = "list"
                    self.fv.index = 0
                    self._render_filters_list()
            else:
                self._filters_move(1)
        elif k == "backspace":
            event.stop()
            if self.fv.focus == "search":
                self._filters_backspace()
        elif k == "escape":
            event.stop()
            if self.fv.query:
                self.fv.query = ""
                self.fv.focus = "search"
                self.fv.index = 0
                self.fv.top = 0
                self._render_filters_list()
            else:
                self._leave_filters(commit=False)
        elif k in ("space", "enter"):
            event.stop()
            if k == "space":
                if self.fv.focus == "search":
                    # 输入框获得焦点时，空格始终是查询内容。
                    self._filters_type(" ")
                else:
                    self._filters_toggle()
            elif self.fv.focus == "list":
                self._leave_filters(commit=True)
            else:  # 搜索框 Enter 只负责无结果时添加自定义条件
                items = self._filters_items()
                if self.fv.query and not items and self._dim_custom_allowed():
                    self._filters_add_custom(self.fv.query)
                    self.fv.query = ""
                    self.fv.index = 0
                    self.fv.top = 0
                    self._render_filters_list()
        else:
            # 其它可打印字符 → 输入即筛（含中文）。Textual 的
            # ``character`` 是实际输入字符，不能从命名键 ``key`` 反推符号。
            text = event.character
            if text and event.is_printable and self.fv.focus == "search":
                event.stop()
                self._filters_type(text)
        # 其余按键在本页不生效，保持固定语义

    def _filters_add_custom(self, value: str) -> None:
        value = value.strip()
        if not value:
            return
        gid = GROUPS[self.fv.dim][0]
        if gid == "categories":
            # 课程类别只能来自自动探测（候选列表），不接受手输
            self.log_line("课程类别只能从自动探测的候选中选择，不能手动添加")
            return
        lst = getattr(self.cfg.filters, gid)
        if value in lst:
            self.log_line(f"已在筛选中：{value}")
        else:
            lst.append(value)
            if value not in self.candidate_lists[gid]:
                self.candidate_lists[gid].append(value)
            self.log_line(f"已添加筛选条目：{value}")

    def _main_key(self, k: str, event: events.Key) -> None:
        if k in ("space",):
            event.stop()
            self._toggle_monitor()
        elif k == "r":
            event.stop()
            self._request_round()
        elif k == "1":
            event.stop()
            self._set_view("all")
        elif k == "2":
            event.stop()
            self._set_view("matched")
        elif k == "3":
            event.stop()
            self._set_view("seats")
        elif k == "f":
            event.stop()
            self._show("filters")
        elif k == "s":
            event.stop()
            self._show("settings")
        elif k == "l":
            event.stop()
            self._show("logs")
        elif k == "h" or k == "?" or k == "question":
            event.stop()
            self._show("help")
        elif k == "q":
            event.stop()
            self.action_quit()
        elif k in ("/", "slash") and self.courses:
            event.stop()
            self._begin_search()
        elif k == "tab" and self.main.search_col:
            event.stop()
            self._search_cycle_col(1)
        elif k == "escape" and self.main.search_col:
            event.stop()
            self._leave_search()
        elif k == "up":
            event.stop()
            self._move_cursor(-1)
        elif k == "down":
            event.stop()
            self._move_cursor(1)
        elif k in ("pageup", "pagedown"):
            event.stop()
            self._handle_cursor_nav(k)
        elif k in ("[", "left_square_bracket"):
            event.stop()
            self._move_page_cursor(-1)
        elif k in ("]", "right_square_bracket"):
            event.stop()
            self._move_page_cursor(1)
        elif k == "enter":
            event.stop()
            self._open_detail()

    def _move_cursor(self, step: int) -> None:
        rows = self._visible_rows()
        if not rows:
            return
        self.main.index = min(max(0, self.main.index + step), len(rows) - 1)
        self._render_course_window(rows)

    def _handle_cursor_nav(self, key: str) -> None:
        """↑↓ 移动一行，PgUp/PgDn 按真实可视高度翻页（查找浏览与主页通用）。"""
        page = self._course_avail()
        step = {"down": 1, "up": -1,
                "pagedown": page, "pageup": -page}.get(key, 0)
        if step:
            self._move_cursor(step)

    def _move_page_cursor(self, step: int) -> None:
        """按课程所在页跳转；每个目标页选择该页第一门课程，并循环。"""
        rows = self._visible_rows()
        if not rows:
            return
        self.main.index = min(max(0, self.main.index), len(rows) - 1)

        # 使用当前过滤/查找结果中的出现顺序，而不是假定页码连续。
        page_order: list[int] = []
        for course in rows:
            page = course.page or 0
            if page not in page_order:
                page_order.append(page)
        if len(page_order) <= 1:
            self.main.index = next(
                (i for i, course in enumerate(rows)
                 if (course.page or 0) == page_order[0]),
                self.main.index,
            )
        else:
            current_page = rows[self.main.index].page or 0
            current_pos = page_order.index(current_page)
            target_page = page_order[(current_pos + step) % len(page_order)]
            self.main.index = next(
                i for i, course in enumerate(rows)
                if (course.page or 0) == target_page
            )
        # 光标定位到本页首门课，并让它落在可视区域第一行（而非掉到底部）
        self.main.top = self.main.index
        self._render_course_window(rows)

    def _open_detail(self) -> None:
        rows = self._visible_rows()
        if not rows or not (0 <= self.main.index < len(rows)):
            return
        self._detail_course = rows[self.main.index]
        self._show("detail")

    def _set_view(self, v: str) -> None:
        if self.main.view != v:
            self.main.view = v
            self.main.reset_cursor()
            self._render_main(force=True)

    def _setup_key(self, k: str, event: events.Key) -> None:
        # 配置表单统一约定：Enter=当前项局部动作（编辑），Ctrl+S=保存整页，Esc=放弃/退出
        if k == "enter":
            event.stop()
            self._setup_edit_email()
        elif k == "ctrl+s":
            event.stop()
            if not self.cfg.notify.to:
                self.log_line("请先填写收件邮箱")
                self._setup_edit_email()
                return
            self._finish_setup()
        elif k == "escape":
            event.stop()
            self.action_quit()  # 首启 Esc = 退出程序

    def _finish_setup(self) -> None:
        self.cfg.first_run_done = True
        self.cfg.save()
        self.log_line("配置完成，可以开始监控（主页按空格）")
        self._show("main", force=True)

    def _settings_key(self, k: str, event: events.Key) -> None:
        if k == "up":
            event.stop()
            self._settings_move(-1)
        elif k == "down":
            event.stop()
            self._settings_move(1)
        elif k in ("left", "right"):
            _g, f = self.s_rows[self.sv.index]
            if f["kind"] == "enum":
                event.stop()
                self._settings_cycle(f, -1 if k == "left" else 1)
        elif k in ("enter",):
            _g, f = self.s_rows[self.sv.index]
            event.stop()
            if f["kind"] == "action":
                self._test_mail_enter()
            elif f["kind"] == "enum":
                self._settings_cycle(f, 1)
            else:
                self._settings_edit(f)
        elif k == "ctrl+s":
            event.stop()
            self._settings_save()
        elif k == "escape":
            event.stop()
            self._leave_settings(commit=False)

    def _test_mail_enter(self) -> None:
        """发送测试邮件：首次回车进入二次确认（动作行转为「再次回车确认发送」），
        第二次回车进入**后台异步发送**——先立即显示「发送中…」，绝不阻塞 UI 线程；
        完成后显示成功/失败，2 秒后自动恢复。"""
        if self.sv.test_mail_sending:
            return   # 已在发送中，忽略再次回车

        if self.sv.test_mail_armed_at is not None:
            # 确认窗口内的第二次回车 → 后台发送
            self.sv.test_mail_armed_at = None
            self.sv.test_mail_sending = True
            self.sv.test_mail_result = None
            self._render_settings_list()      # 先重绘为「发送中…」，再启动后台线程
            # 提前取不可变参数，避免后台线程与 UI 线程编辑中的 draft 竞态
            to = self.sd.get("to", "").strip()
            gws_from = self.sd.get("gws_from", "").strip()
            self._send_test_mail_worker(to, gws_from)
            return

        self.sv.test_mail_armed_at = time.time()
        self.set_timer(self.TEST_MAIL_CONFIRM_TIMEOUT_S,
                       self._expire_test_mail_confirm)
        self._render_settings_list()

    def _expire_test_mail_confirm(self) -> None:
        """2 秒内没有再按回车：取消发送，恢复原操作提示。"""
        if self.sv.test_mail_armed_at is not None:
            self.sv.test_mail_armed_at = None
            self.log_line("测试邮件未发送（超时取消）")
            if self.page == "settings":
                self._render_settings_list()

    def _send_test_mail_draft(self, to: str, gws_from: str) -> bool:
        """后台线程内安全地真正发送测试邮件，返回成败。

        只使用调用方传入的不可变 draft 参数，绝不读 self.sd，避免与 UI 线程
        编辑中的 draft 竞态；日志经 _thread_log 回主线程，不在后台线程碰 UI。
        """
        if not to:
            self._thread_log("✗ 收件邮箱为空，无法测试")
            return False
        if not notifier.gws_available():
            self._thread_log("✗ gws 未安装，无法发送")
            return False
        n = self.cfg.notify
        self._thread_log("正在发送测试邮件…（使用当前改动，尚未保存）")
        ok = notifier.send_email_with_retry(
            type(n)(to=to, gws_from=gws_from,
                    min_interval_min=n.min_interval_min,
                    max_per_hour=n.max_per_hour),
            "【courser】测试邮件",
            "courser 测试邮件：设置里的改动尚未保存，此测试不落盘。",
            log=self._thread_log)
        self._thread_log("测试邮件已发送" if ok
                         else "✗ 测试邮件发送失败（已重试 3 次），请检查 gws / 网络")
        return ok

    @work(thread=True, exclusive=True)
    def _send_test_mail_worker(self, to: str, gws_from: str) -> None:
        ok = self._send_test_mail_draft(to, gws_from)
        self.call_from_thread(self._finish_test_mail, ok)

    def _finish_test_mail(self, ok: bool) -> None:
        self.sv.test_mail_sending = False
        self.sv.test_mail_result = ok
        if self.page == "settings":
            self._render_settings_list()
        self.set_timer(self.TEST_MAIL_RESULT_CLEAR_S, self._clear_test_mail_result)

    def _clear_test_mail_result(self) -> None:
        self.sv.test_mail_result = None
        if self.page == "settings":
            self._render_settings_list()

    # ------------------------------------------------------------------
    # 动作
    # ------------------------------------------------------------------
    def _toggle_monitor(self) -> None:
        w = self.watcher
        if w is None:
            return
        if not self.cfg.first_run_done:
            self.log_line("请先完成收件邮箱与 gws 授权设置")
            self._show("setup")
            return
        if w.running:
            w.stop()
            self.log_line("监控已停止")
        else:
            w.start()
            self._start_live_ticker()   # 监控等待的下一轮倒计时也要实时刷新
            self.log_line(f"监控开始：每约 {self.cfg.interval_min} 分钟一轮（带抖动）")

    @work(thread=True, exclusive=True)
    def _run_round(self) -> None:
        w = self.watcher
        if w is None:
            return
        self._thread_log("手动触发一轮抓取…")
        w.run_round()

    def _request_round(self) -> None:
        """从 UI 线程发起抓取：先启动实时计时，再把整轮工作交给后台线程。"""
        self._start_live_ticker()
        self._run_round()

    def _on_round(self, r: RoundResult) -> None:
        if self._shutting_down:
            return
        try:
            self.call_from_thread(self._apply_round, r)
        except RuntimeError:
            pass

    def _apply_round(self, r: RoundResult) -> None:
        # 本轮结束：清掉进行中的进度，避免残留"正在读取…"
        self.prog.clear()
        # 风控摘要每轮都更新（不论 r.ok、不论有没有课程）——风控命中而本轮无课程
        # /失败时，正是最需要刷新风控历史的地方；来源是 RiskHistory 独立历史。
        self.main.risk_summary = (r.risk_percent, r.risk_label,
                                  r.risk_hits, r.risk_total)
        if r.ok and r.courses:
            self.courses = r.courses
            self.candidate_lists = self.snapshot.save(r)
            self.main.snapshot_ts = time.strftime("%Y-%m-%d %H:%M:%S")
            self.main.snapshot_meta = f"{r.pages} 页 · {len(r.courses)} 门课程"
        if self.fv.gathering:
            self._on_gather_done()
        if self.page == "main":
            self._render_main(force=True)
        elif self.page == "filters":
            self._render_filters_list()
        self._render_runstate()
        if r.notified:
            names = "、".join(c.name for c in r.notified)
            self.log_line(f"[green]已发送提醒邮件：{names}[/]")

    def on_unmount(self) -> None:
        self._shutting_down = True
        if self.watcher:
            self.watcher.stop()

    def action_quit(self) -> None:
        """发起非阻塞退出；后台 I/O 收尾完成后再结束 TUI。"""
        if self._shutting_down:
            return
        self._shutting_down = True
        self._shutdown_message = "收到取消请求…正在停止浏览器操作…"
        self._render_runstate()
        self.run_worker(self._quit_after_stop(), exclusive=True,
                        name="courser-shutdown")

    async def _quit_after_stop(self) -> None:
        """在不阻塞 Textual 事件循环的情况下等待监控线程收尾。"""
        if self.watcher:
            # stop() 会 request_stop 后 join；放到线程池，避免冻结 TUI 及其退出提示。
            await asyncio.to_thread(self.watcher.stop)
        self._shutdown_message = "✓ 已停止"
        self._render_runstate()
        # 给 Textual 一个事件循环周期，确保最终状态能真正绘制出来。
        await asyncio.sleep(0.05)
        self.exit()

    @on(events.TextSelected)
    def _auto_copy_selection(self, event: events.TextSelected) -> None:
        """鼠标拖动选中文字、松手即自动复制：TextSelected 由 Textual 在 MouseUp
        后发出，这里取出 `screen.get_selected_text()` 的纯文本，经 OSC 52 写入
        剪贴板，并用不占布局的 toast 提示（不改变当前页面 / 光标 / 滚动状态）。
        空 selection 直接忽略，不要求用户再按复制键。"""
        text = self.screen.get_selected_text()
        if not text:
            return
        self.copy_to_clipboard(text)
        lines = text.count("\n") + 1
        msg = (f"✓ 已复制 {lines} 行" if lines > 1
               else f"✓ 已复制 {len(text)} 个字符")
        self.notify(msg, title="", timeout=1.5)

def _render_once_result(out: Console, r: RoundResult) -> None:
    """`courser --once` 的结果展示：汇总 Panel + 命中课程 Table 全部走 Rich。

    独立成函数便于无头测试渲染；非 TTY 时 Rich 自动去色/去框。
    """
    from rich.markup import escape
    seats = [c for c in r.matched if c.has_seats]
    ok = r.ok
    summary = Text()
    summary.append("页数 ", style="dim"); summary.append(f"{r.pages}", style="bold")
    summary.append("  课程 ", style="dim"); summary.append(f"{r.total}", style="bold")
    summary.append("  命中 ", style="dim"); summary.append(f"{len(r.matched)}", style="bold")
    summary.append("  空余 ", style="bold")
    summary.append(f"{len(seats)}", style="bold green" if seats else "bold")
    out.print(Panel(
        summary,
        title=ui_title(" 补退选空余名额 "),
        border_style=ACCENT,
        subtitle=f"状态：[{('green' if ok else 'red')}]● {'成功' if ok else '失败'}[/]",
        padding=(0, 1),
    ))

    if r.matched:
        table = Table(show_header=True, header_style=f"bold {ACCENT}",
                      box=None, show_edge=False, pad_edge=False, padding=(0, 2))
        for key, label in (("name", "课程名"), ("no", "课程号"), ("cat", "类别"),
                           ("dept", "开课单位"), ("seats", "限选/已选"),
                           ("avail", "空余"), ("status", "状态")):
            table.add_column(label, justify=("right" if key in ("no", "seats", "avail") else "left"))
        for c in ordered_courses(r.matched):
            avail = course_display(c, "avail")
            avail_markup = f"[green]{avail}[/]" if c.has_seats else f"[dim]{avail}[/]"
            table.add_row(
                escape(c.name or "—"), escape(c.course_no or "—"),
                escape(c.category or "—"), escape(c.dept or "—"),
                seats_display(c), avail_markup, escape(c.status or "—"),
            )
        out.print(table)
    else:
        out.print(ui_meta("本轮无命中课程。"))


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="courser",
                                 description="PKU 补退选空余名额监控（纯文本 TUI）")
    ap.add_argument("--once", action="store_true",
                    help="不启动 TUI，直接执行一轮抓取并打印结果（便于 cron/调试）")
    ap.add_argument("--verbose", action="store_true",
                    help="--once 时展示完整 workflow 明细（默认只显示高层阶段）")
    args = ap.parse_args(argv)

    load_env_file()
    cfg = Config.load()

    if args.once:
        # 非 TUI 的一次性运行：进度/日志走 stderr，结果走 stdout。
        # TTY 下用 Rich Live 呈现「阶段状态区」（原地更新），
        # 非 TTY（cron / 重定向）回退为逐行文本 + [courser] 前缀。
        err = Console(stderr=True, highlight=False)
        out = Console(highlight=False)
        renderer = make_once_renderer(err, out, args.verbose)
        r = None
        try:
            sched = MonitorScheduler(cfg, log=renderer.log,
                                     on_progress=renderer.progress)
            r = sched.run_round()
            renderer.finish(r)
        finally:
            renderer.stop()
        _render_once_result(out, r)
        return 0 if (r is not None and r.ok) else 2

    # mouse=True：由 Courser/Textual 接管鼠标——拖动=选择，松手=自动复制（OSC52），
    # 并恢复 Settings / Help / 日志 / 详情页的滚轮滚动。Textual 内部 selection 是
    # 唯一复制路径，不再依赖终端原生 selection（见 tests/tui/test_text_selection.py）。
    CourserApp(cfg).run(mouse=True)
    return 0

if __name__ == "__main__":
    import sys
    sys.exit(main())
