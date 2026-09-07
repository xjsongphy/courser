"""courser 的纯文本监控 TUI。

定位：一个 **persistent status monitor**，而不是塞进终端的桌面应用。主页在
常态下只呈现当前状态、关注的课程与最近一次事件；所有配置都通过少量二级页面
完成。页面用线条边框面板分区，配色克制（统一原语见 courser/tui/theme.py）。

交互规范（一种操作一种入口，一个键一种语义）：
- Enter = 进入 / 确认；Esc = 返回 / 放弃（**Esc 任何地方都不保存**）；
- ↑↓ = 移动，Space = 开始/停止或选中/取消（各页内遵循）；
- 主页键：Space 开始/停止 · r 立即抓取 · f 筛选 · s 设置 · l 日志 ·
  1/2/3 视图（全部/符合筛选/只看空余）· h 帮助 · q 退出。
所有文字输入（设置字段、首启收件邮箱等）都复用同一套行内编辑器
（见 courser/editing.py）：值保留在原行、回车进入、←/→ 移动光标、回车确认、
Esc 取消；没有独立输入框。筛选页的搜索则直接输入即过滤。
"""

from __future__ import annotations

import argparse
import copy
import time
from math import ceil
from typing import Optional

from rich.cells import cell_len
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical
from textual.widgets import Static

from .. import notifier
from ..config import Config, load_env_file
from ..editing import FieldEditor
from ..filters import FilterSet
from ..models import Course, RoundResult
from ..scheduler import MonitorScheduler
from ..storage import SnapshotStore
from .state import (EditingState, FilterViewState, MainViewState,
                    ProgressState, SettingsViewState)
from .theme import (COURSE_DETAIL_FIELDS, CSS, GROUPS, SEP, SETTINGS_FIELDS,
                    FocusScroll, FocusableStatic, _hint, _pad,
                    field_mutate, field_value, kv_row, page_hint, plain_markup,
                    risk_markup, shortcut_row, ui_error, ui_key, ui_label,
                    ui_meta, ui_ok, ui_section, ui_title, ui_value, ui_warn)

# 为兼容旧内部命名（保持渲染/按键逻辑可读性，缩写映射到 theme/state 的统一实现）
_risk_markup = risk_markup
_kv_row = kv_row
_shortcut_row = shortcut_row
_page_hint = page_hint
_plain_markup = plain_markup
_field_value = field_value
_field_mutate = field_mutate

# 全局活动状态栏（底部唯一 activity 行）展示的页面；其它页面隐掉，正文各自负责。
_ACTIVITY_PAGES = {"main", "filters", "logs", "detail"}

class CourserApp(App):
    """纯文本菜单 TUI：主页面 + 筛选/设置/日志/帮助/详情/首启设置。"""

    TITLE = "courser"
    SUB_TITLE = "PKU 补退选空余名额监控"
    CSS = CSS
    # 复制完全交给终端，不使用 Textual 的应用内文本选择/剪贴板。
    ALLOW_SELECT = False
    BINDINGS = [Binding("ctrl+c", "quit", "退出", show=False, priority=True)]

    def __init__(self, cfg: Config):
        # 空白区域使用终端自身的默认背景/调色板，不铺 Textual 深色主题底。
        super().__init__(ansi_color=True)
        self.cfg = cfg
        self.page = "main"
        self.log_buf: list[str] = []
        self.courses: list[Course] = []
        self.candidate_lists = {"names": [], "categories": [], "depts": []}
        self.watcher: Optional[MonitorScheduler] = None
        self.snapshot = SnapshotStore()
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
        self.call_from_thread(self.log_line, msg)

    # ------------------------------------------------------------------
    # 状态文本（数据与排版分离：先取语义数据，再套统一原语）
    # ------------------------------------------------------------------
    def _last_round_parts(self):
        """上一轮的语义数据（不含排版）。
        返回 None 表示还没有任何一轮；否则返回
        (ok, primary, meta, status, detail)：
          ok=True  : primary='5 页 / 105 课' meta='196s' status='成功' detail=''
          ok=False : primary='' meta='' status='失败' detail=<错误纯文本>
        """
        r = self.watcher.last_result if self.watcher else None
        if r is None:
            return None
        if r.ok:
            return (True, f"{r.pages} 页 / {r.total} 课",
                    f"{r.duration_s:.0f}s", "成功", "")
        return (False, "", "", "失败", r.error or "")

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

    def _last_round_markup(self, parts=None) -> str:
        """把 _last_round_parts 的结果套上排版（不含外部 label）。"""
        if parts is None:
            parts = self._last_round_parts()
        if parts is None:
            return ui_meta("—")
        ok, primary, meta, status, detail = parts
        if ok:
            seg = [ui_value(primary)]
            if meta:
                seg.append(ui_meta(meta))
            seg.append(ui_ok(status))
            return "  ".join(seg)
        return ui_error(self._error_summary(detail or status))

    def _mail_markup(self) -> str:
        if not notifier.gws_available():
            return ui_warn("gws 未安装")
        if not self.cfg.notify.to:
            return ui_warn("收件邮箱未填写")
        return ui_ok("✓ 已就绪")

    def _google_markup(self) -> str:
        """按最近一次发信结果展示 Google/邮件连通性。"""
        ok, _ts = notifier.last_mail_status()
        if ok is None:
            return ui_meta("— 尚未发送过邮件")
        if ok:
            return ui_ok("✓ 可达（上次发送成功）")
        return ui_error("✗ 不可达（上次发送失败）")

    def _google_status_markup(self) -> str:
        """底部状态栏的紧凑 Google 连通性标记。"""
        ok, _ts = notifier.last_mail_status()
        if ok is None:
            return ui_value("—")
        return ui_ok("✓") if ok else ui_error("✗")

    def _gmail_footer(self) -> str:
        """状态行最末的发件通道标记，按连通性染色：
        灰=还没发过邮件（中性）、绿=上次发送成功、红=上次发送失败。"""
        ok, _ts = notifier.last_mail_status()
        if ok is None:
            return ui_meta("gmail")
        return ui_ok("gmail") if ok else ui_error("gmail")

    def _labeled(self, label: str, content: str) -> str:
        """主页 summary 的一行：左侧 label 退后并对齐，右侧 content 自带排版。"""
        # 这是 dashboard 的字段标题，不是辅助说明；不能和右侧 meta 一起 dim。
        return f"{ui_section(_pad(label, 6))}  {content}"

    def _monitor_summary(self) -> list[str]:
        w = self.watcher
        lines = []
        # 运行状态与上一轮结果由底部固定 runstate 统一展示；这里仅保留
        # 主页上下文（筛选 / 通知 / 风控），避免同一状态在上下两处重复。
        fs = self.cfg.filters
        if fs.empty:
            lines.append(self._labeled(
                "筛选", ui_meta("未配置（不会告警）")))
        else:
            group_txt = SEP.join(
                f"{ui_value(n)}×{len(v)}" for n, v in fs.active_groups)
            mode = "任一" if fs.match != "all" else "全部"
            lines.append(self._labeled(
                "筛选", f"{group_txt}  {ui_meta('满足' + mode + '条件')}"))
        lines.append(self._labeled("通知", self._mail_markup()))
        if w and w.last_result:
            risk = _risk_markup(w.last_result.risk_percent,
                                w.last_result.risk_label)
            if risk:
                lines.append(self._labeled("风控", risk))
        return lines


    # ------------------------------------------------------------------
    # 课程行 / 自适应列
    # ------------------------------------------------------------------
    def _visible_rows(self) -> list[Course]:
        fs = FilterSet(self.cfg.filters)
        rows = self.courses
        if self.main.view == "matched":
            rows = [c for c in rows if fs.matches(c)]
        elif self.main.view == "seats":
            rows = [c for c in rows if c.has_seats]
        if self.main.search_col:
            q = self._search_effective_query().strip().lower()
            if q:
                rows = [c for c in rows
                        if q in self._row_search_text(c, self.main.search_col).lower()]
        return rows

    # -- 主页按列查找（1/2/3 三个视图通用，不分工况）---------------------
    def _search_cols(self) -> list[tuple[str, str]]:
        """当前可见列（含中文标签）；查找列只能在这些列里选，
        保证被查找列的列名始终高亮可见。"""
        return [(k, l) for k, l, _w in self._columns(max(40, self.size.width - 4))]

    def _search_col_label(self) -> str:
        for k, l in self._search_cols():
            if k == self.main.search_col:
                return l
        return self.main.search_col or ""

    def _begin_search(self) -> None:
        """启动查找：默认按课程名列，弹出输入框；输入即筛。"""
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

    def _search_effective_query(self) -> str:
        """编辑中输入即筛：取编辑器缓冲；否则取已生效的查找词。"""
        if self.editing.context == "search":
            return self.editor.text
        return self.main.search_query

    def _clear_search(self) -> None:
        self.main.search_col = None
        self.main.search_query = ""

    def _leave_search(self) -> None:
        """一次 Esc 退出查找，包括正在编辑的输入态。"""
        self._clear_search()
        if self.editing.context == "search":
            self._clear_editing()
        else:
            self.editor.reset()
        self._render_main(force=True)

    def _main_hint(self) -> str:
        """主页底部只显示当前状态下真正可用的操作。"""
        if self.editing.context == "search":
            return _hint(
                ("Tab", "换列"), ("Enter", "确认查找"),
                ("Esc", "退出查找"),
            )
        if self.main.search_col:
            return _hint(
                ("Enter", "编辑查找"), ("Tab", "换列"),
                ("Esc", "退出查找"),
            )
        # 基础操作：没有可展示的数据时，不提示需要数据才能用的操作（如查找/视图）
        parts: list[tuple[str, str]] = [("空格", "开始/停止"), ("r", "立即抓取")]
        if self.courses:
            parts += [("/", "按列查找")]
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
        value = (self.editor.markup()
                 if self.editing.context == "search"
                 else ui_value(self.main.search_query or "（未输入）"))
        si.update("\n".join([
            ui_section("查找"),
            _kv_row("列", label, width=6),
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
        if key == "seats":
            return c.seats_raw or (f"{c.selected}/{c.quota}"
                                   if c.quota is not None else "—")
        if key == "avail":
            return str(c.avail) if c.avail >= 0 else "—"
        return ""

    def _columns(self, W: int) -> list[tuple[str, str, int]]:
        """选择自适应列宽；文本列超宽时折行，不用省略号。"""
        pre = [("page", "页", 3), ("no", "课程号", 10)]
        fixed = [("seats", "限选/已选", 8), ("avail", "空余", 6)]
        opt = [("cat", "课程类别", 16), ("dept", "开课单位", 14),
               ("teacher", "教师", 16)]
        min_name = 6
        # 3 格光标槽 + 所有列间分隔空格。
        fixed_width = 3 + sum(w for _k, _l, w in pre + fixed)
        fixed_width += len(pre) + len(fixed)
        budget = max(min_name, W - fixed_width)
        picked: list[tuple[str, str, int]] = []
        for key, label, width in opt:
            if width + 1 + min_name <= budget:
                picked.append((key, label, width))
                budget -= width + 1
        name_w = min(24, max(1, budget))
        return pre + [("name", "课程", name_w)] + picked + fixed

    def _table_width(self, cols) -> int:
        return 3 + sum(width for _key, _label, width in cols) + len(cols) - 1

    def _header_labels(self, cols, indent: int = 0) -> str:
        centered = {"page", "cat", "seats", "avail"}
        cells = []
        for key, label, width in cols:
            justify = "center" if key in centered else "left"
            cell = self._align_cell(label, width, justify)
            # 查找列列名高亮（启动查找后始终可见）；其余保持 section 灰
            if self.main.search_col and key == self.main.search_col:
                cells.append(f"[bold cyan]{cell}[/]")
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
        """渲染一门课程的物理行；折行时其它列保持垂直对齐。"""
        values = {
            "page": str(c.page) if c.page else "—",
            "no": c.course_no or "—",
            "name": c.name or "—",
            "cat": c.category or "—",
            "dept": c.dept or "—",
            "teacher": c.teacher or "—",
            "seats": c.seats_raw or (f"{c.selected}/{c.quota}"
                                      if c.quota is not None else "—"),
            "avail": "—" if c.avail < 0 else str(c.avail),
        }
        wrapped: dict[str, list[str]] = {}
        for key, _label, width in cols:
            cell_width = width - 2 if key == "name" and matched else width
            wrapped[key] = self._wrap_text(values[key], cell_width)
        height = max(len(parts) for parts in wrapped.values())
        centered = {"page", "cat", "seats", "avail"}
        output = []
        for line_no in range(height):
            cells = []
            for key, _label, width in cols:
                parts = wrapped[key]
                text = parts[line_no] if line_no < len(parts) else ""
                justify = "center" if key in centered else "left"
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
            output.append(" " * indent + cursor_cell + " ".join(cells).rstrip())
        return output

    # ------------------------------------------------------------------
    # compose / 生命周期
    # ------------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield FocusableStatic("", id="brand")
        with Vertical(id="stage"):
            # 主页
            with Vertical(id="page-main"):
                yield Static("", id="summary", markup=True)
                yield Static("", id="coursehead", markup=True)
                yield Static("", id="searchinput", markup=True)
                yield Static("", id="snapshot", markup=True)
                yield Static("", id="courselist", markup=True)
            # 筛选（pi 式：顶部输入即筛 + 列表，↑↓ 选，空格/回车 切换）
            with Vertical(id="page-filters"):
                yield Static("", id="filters_hdr", markup=True)
                yield Static("", id="fsearch", markup=True)
                yield Static("", id="filters_list", markup=True)
            # 设置（行内编辑，无独立输入框）
            with Vertical(id="page-settings"):
                yield Static("", id="settings_hdr", markup=True)
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
        self._show("main")
        self._render_status_ticker()
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
        "filters": _page_hint("Tab 切换维度 · ↑↓ 移动 · 空格 选中 · 回车 保存 · Esc 放弃"),
        "settings": _page_hint("↑↓ 选择 · 回车 编辑 · ←→ 切换 · Ctrl+S 保存 · Esc 放弃"),
        "logs": _page_hint("↑↓ / PgUp / PgDn 滚动 · Esc 返回"),
        "help": _page_hint(),
        "detail": _page_hint(),
        "setup": _page_hint("↓ 编辑收件邮箱 · 回车 继续 · Esc 退出程序"),
    }

    def _page_ids(self):
        return ["main", "filters", "settings", "logs", "help", "detail", "setup"]

    def _anchor_focus(self) -> None:
        """把焦点放到顶部惰性锚（不消费任何键），让页面键路由接管。"""
        try:
            self.query_one("#brand", FocusableStatic).focus()
        except Exception:
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
            self._anchor_focus()
        else:
            self._anchor_focus()
            self._render_main(force=True)
        keys = self.query_one("#keys", Static)
        keys.display = page == "main"
        if page == "main":
            keys.update(self._main_hint())
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
        self.query_one("#brand", Static).update(
            ui_section("courser") + ui_meta(" — PKU 补退选空余名额监控"))
        # “课程”与筛选/通知使用同一标签列，选项内容从同一列起始。
        options = " ".join(
            [("[bold cyan]1 全部[/]" if self.main.view == "all" else "[dim]1 全部[/]"),
             ("[bold cyan]2 符合筛选[/]" if self.main.view == "matched" else "[dim]2 符合筛选[/]"),
             ("[bold cyan]3 只看空余[/]" if self.main.view == "seats" else "[dim]3 只看空余[/]")])
        self.query_one("#coursehead", Static).update(self._labeled("课程", options))

        snapshot = ""
        if self.main.snapshot_ts:
            snapshot = ui_section("最近一次") + "\n  " + ui_value(self.main.snapshot_ts)
            if self.main.snapshot_meta:
                snapshot += f" · {ui_value(self.main.snapshot_meta)}"
        self.query_one("#snapshot", Static).update(snapshot)
        summary = "\n".join(self._monitor_summary())
        self.query_one("#summary", Static).update(summary)

        rows = self._visible_rows()
        # 课程窗口为底部可换行的操作提示、状态栏和“最近一次”元信息让出空间。
        hint_width = max(1, self.size.width - 4)
        hint_plain = _plain_markup(self._main_hint())
        hint_lines = sum(max(1, ceil(cell_len(line) / hint_width))
                         for line in hint_plain.splitlines() or [""])
        snapshot_extra = 1 if snapshot else 0
        # 查找区固定为标题 + “列” + “输入”三行；底部提示另行渲染，
        # 不再把快捷键挤在查找控件旁边。
        search_extra = 3 if self.main.search_col else 0
        avail_h = max(1, self.size.height - 17 - snapshot_extra - search_extra
                      - max(0, hint_lines - 1))
        self._render_course_window(rows, avail_h)
        self._render_search_line()
        self.query_one("#keys", Static).update(self._main_hint())

    def _render_main_lite(self) -> None:
        """主页的轻量刷新（只刷底部全局活动状态行），不做整页重排。"""
        if self.page != "main":
            return
        self._render_runstate()

    def _render_course_window(self, rows: list[Course], avail: int) -> None:
        body = self.query_one("#courselist", Static)
        if not rows:
            # 抓取已经开始后，进度区已经说明当前发生了什么；列表区保持安静，
            # 不再重复显示“按 r / 空格开始”的启动提示。
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
        if self.main.index >= len(rows):
            self.main.index = len(rows) - 1
        if self.main.index < self.main.top:
            self.main.top = self.main.index
        if self.main.index >= self.main.top + avail:
            self.main.top = self.main.index - avail + 1
        W = max(40, body.size.width or self.size.width - 8)
        cols = self._columns(W)
        table_width = self._table_width(cols)
        indent = max(0, (W - table_width) // 2)
        fs = FilterSet(self.cfg.filters)
        def build(start: int):
            rendered = []
            used = 1  # 表头
            for i in range(start, len(rows)):
                course_lines = self._course_lines(
                    rows[i], fs.matches(rows[i]), cols,
                    cursor=i == self.main.index, indent=indent)
                if rendered and used + len(course_lines) > avail:
                    break
                rendered.append((i, course_lines))
                used += len(course_lines)
            return rendered

        # 光标必须始终出现在当前物理窗口内；长文本折行后按真实行数分页。
        self.main.top = min(self.main.top, self.main.index)
        visible = build(self.main.top)
        while visible and self.main.index not in {i for i, _ in visible} \
                and self.main.top < self.main.index:
            self.main.top += 1
            visible = build(self.main.top)

        lines = [self._header_labels(cols, indent)]
        for i, course_lines in visible:
            c = rows[i]
            lines.extend(course_lines)
        try:
            body.update("\n".join(lines))
        except Exception:
            # 兜底：绝不让任意课程文本触发 markup 解析错误而崩掉 TUI，降级为纯文本
            body.update("\n".join(ui_value(_plain_markup(ln)) for ln in lines))

    # ------------------------------------------------------------------
    # 渲染：筛选页
    # ------------------------------------------------------------------
    def _begin_filters(self) -> None:
        self._f_backup = copy.deepcopy(self.cfg.filters)  # Esc 放弃依据
        self.fv.dim = 0
        self.fv.query = ""
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
        self._run_round()

    def _on_gather_done(self) -> None:
        self.fv.gathering = False
        if self.page == "filters":
            self._render_filters_list()

    def _filters_entries(self) -> list[str]:
        return list(getattr(self.cfg.filters, GROUPS[self.fv.dim][0]))

    def _filters_dim_cycle(self) -> None:
        self.fv.dim = (self.fv.dim + 1) % len(GROUPS)
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
            fs.update("\n".join([
                _kv_row("维度", ui_key(dim_name)),
                _kv_row("组合", ui_value(mode)),
                _kv_row("已选", ui_value(str(len(entries)))),
                "",
                f"{ui_key('搜索')}  {q}[cyan]▍[/]"
                + (f"  {ui_meta('输入文字可过滤下方列表')}" if not self.fv.query else ""),
            ]))
        items = self._filters_items()
        listw = self.query_one("#filters_list", Static)
        if not items:
            if self.fv.gathering:
                listw.update(_page_hint(
                    "Tab 切换维度 · ↑↓ 移动 · 空格 选中 · 回车 保存 · Esc 放弃"))
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
            listw.update("\n".join([ui_meta(message), "", _page_hint(
                "Tab 切换维度 · ↑↓ 移动 · 空格 选中 · 回车 保存 · Esc 放弃")]))
            return
        if self.fv.index >= len(items):
            self.fv.index = len(items) - 1
        if self.fv.index < self.fv.top:
            self.fv.top = self.fv.index
        maxlines = max(3, min(20, self.size.height - 12))
        if self.fv.index >= self.fv.top + maxlines:
            self.fv.top = self.fv.index - maxlines + 1
        window = items[self.fv.top:self.fv.top + maxlines]
        cands = set(self.candidate_lists[GROUPS[self.fv.dim][0]])
        out = []
        for i, it in enumerate(window):
            idx = self.fv.top + i
            cur = "[cyan]❯[/]" if idx == self.fv.index else " "
            mark = "[cyan]✓[/]" if it in entries else " "
            extra = " [dim](手动添加)[/]" if it not in cands else ""
            out.append(f"{cur} {mark} {ui_value(it)}{extra}")
        out.extend(["", _page_hint(
            "Tab 切换维度 · ↑↓ 移动 · 空格 选中 · 回车 保存 · Esc 放弃")])
        listw.update("\n".join(out))

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

    def _render_settings_list(self) -> None:
        hdr = self.query_one("#settings_hdr", Static)
        gws_ok = notifier.gws_available()
        gws = ui_ok("✓ 已安装") if gws_ok else ui_warn("✗ 未找到 gws")
        hdr.update(ui_title("设置"))
        group_names = {
            "账号凭据（可选；留空则依赖浏览器密码管理器自动填充）":
                ("账号", "可留空，登录时依赖浏览器密码管理器自动填充"),
            "邮件通知（gws 发送，需先 `gws auth login` 授权）":
                ("邮件", "通过 gws 发送提醒"),
            "轮询节奏（自动带随机抖动）": ("轮询", "自动带随机抖动"),
            "行为": ("浏览器", "会话与窗口行为"),
        }
        out = [f"{ui_label('gws')} {gws}  {ui_meta('t 测试邮件 · Ctrl+S 保存')}\n"]
        last = None
        for i, (gname, f) in enumerate(self.s_rows):
            if gname != last:
                title, note = group_names.get(gname, (gname, ""))
                if last is not None:
                    out.append("")
                out.append(ui_section(title))
                if note:
                    out.append(ui_meta(f"  {note}"))
                last = gname
            cur = "[cyan]❯[/]" if i == self.sv.index else " "
            if (i == self.sv.index and self.editing.context == "settings"
                    and self.editing.key == f["key"]):
                out.append(_kv_row(f["label"], self.editor.markup(),
                                   width=16, prefix=f"{cur} "))
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
        out.extend(["", _page_hint(
            "↑↓ 选择 · 回车 编辑 · ←→ 切换 · Ctrl+S 保存 · Esc 放弃")])
        self.query_one("#settings_list", Static).update("\n".join(out))

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
        """编辑态按键统一走 FieldEditor；commit/cancel 才由调用方落盘。"""
        if self.editing.context == "search" and event.key == "tab":
            event.stop()
            self._search_cycle_col(1)
            return
        out = self.editor.feed(event.key,
                               getattr(event, "char", None),
                               bool(getattr(event, "is_printable", False)))
        event.stop()
        if out == "commit":
            val = self.editor.text
            context, key = self.editing.context, self.editing.key
            if context == "search":
                self.main.search_query = val
            self._clear_editing()
            if context == "settings" and key:
                self.sd[key] = val
            elif context == "setup":
                self.cfg.notify.to = val
                self.log_line("收件邮箱已更新")
            self._render_field_page()
        elif out == "cancel":
            # 查找编辑取消：还原为已生效的查找词，不自动清除整条查找
            self._clear_editing()
            self._render_field_page()
        else:
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
                _field_mutate(self.cfg, f["key"], self.sd.get(f["key"], ""))
            self.cfg.save()
        except ValueError:
            self.log_line("✗ 保存失败：数值越界/格式错误，未写入")
            return
        self.log_line("设置已保存")
        self._leave_settings(commit=True)

    def _leave_settings(self, commit: bool) -> None:
        self._clear_editing()
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
            lines.extend([ui_meta("暂无日志；开始监控或抓取后这里会记录每一轮过程"), "",
                          _page_hint("↑↓ / PgUp / PgDn 滚动 · Esc 返回")])
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
        lines.extend(["", _page_hint("↑↓ / PgUp / PgDn 滚动 · Esc 返回")])
        body.update("\n".join(lines))

    def _render_help(self) -> None:
        lines = [ui_title("帮助"), "", ui_section("主页"),
                 _shortcut_row("Space", "开始 / 停止监控"),
                 _shortcut_row("r", "立即抓取一轮"),
                 _shortcut_row("1", "全部课程"),
                 _shortcut_row("2", "符合筛选"),
                 _shortcut_row("3", "有空余名额"),
                 _shortcut_row("/", "按列查找（输入即筛，Tab 换列）"),
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
                 _shortcut_row("t", "发送测试邮件"),
                 _shortcut_row("Ctrl+S", "保存"),
                 _shortcut_row("Esc", "放弃修改"), "",
                 ui_section("监控流程"),
                 "  登出旧会话", "    → IAAA 登录", "    → 补退选",
                 "    → 动态读取所有页面", "    → 筛选空余课程",
                 "    → 邮件通知", "",
                 ui_meta("每轮重新登录；遇验证码或风控提示时不会持续重试。"),
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
                      _kv_row("所属页", ui_value(str(fields.get("page") or "—")), width=12),
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
                             if gws else "[red]✗[/] gws 未安装（brew install gws）"))
        if gws:
            lines.append("  [yellow]⚠[/] gws 尚未授权——请先在命令行执行 gws auth login")
        lines.extend(["", ui_section("收件邮箱"), ui_meta("用于接收提醒，必填")])
        if self.editing.context == "setup" and self.editing.key == "to":
            lines.append(_kv_row("邮箱", self.editor.markup(), width=12, prefix="❯ "))
        else:
            if has_recip:
                lines.append(_kv_row("邮箱", ui_value(self.cfg.notify.to), width=12, prefix="❯ "))
            else:
                lines.append(_kv_row("邮箱", ui_warn("未填写"), width=12, prefix="❯ "))
            lines.append("  " + ui_meta("按 ↓ 或 回车 编辑收件邮箱"))
        lines.append("")
        lines.append(ui_meta("学号 / 密码可留空：登录时由浏览器密码管理器自动填充。"))
        lines.append(ui_meta("也可稍后在主页按 s，在「设置 → 账号」中补充。"))
        lines.append("")
        if has_recip:
            lines.append(ui_ok("收件邮箱已配置，可以开始使用。"))
        else:
            lines.append(ui_warn("请先填写收件邮箱。"))
        lines.extend(["", _page_hint("↓ 编辑收件邮箱 · 回车 继续 · Esc 退出程序")])
        body.update("\n".join(lines))

    # ------------------------------------------------------------------
    # 首启设置：收件邮箱同样用 FieldEditor 行内编辑（与设置页同一套代码）
    # ------------------------------------------------------------------
    def _setup_edit_email(self) -> None:
        self._begin_field("setup", "to", "text", self.cfg.notify.to)

    # ------------------------------------------------------------------
    # 抓取进度（本轮步骤 + 当前操作实时提示）
    # ------------------------------------------------------------------
    def _thread_progress(self, done, total, op):
        self.call_from_thread(self._set_progress, done, total, op)

    def _set_progress(self, done, total, op):
        self.prog.done, self.prog.total, self.prog.op = done, total, op
        try:
            self._render_runstate()
        except Exception:
            pass

    def _activity_fetching(self, w) -> str:
        """抓取过程中：• 状态 抓取中 · 进度 │ 已运行 Xs │ 来源 Google。"""
        seg = [f"{ui_meta('• 状态')}  {ui_warn('抓取中')}"]
        # 不显示步数，只显示当前正在做什么（登出/登录/读页/等待等）。
        if self.prog.op:
            seg[0] += f" · {ui_value(str(self.prog.op))}"
        elapsed = time.time() - w.current_round_started_at
        seg.append(f"{ui_meta('已运行')} {ui_value(f'{elapsed:.0f}s')}")
        seg.append(self._gmail_footer())
        return " │ ".join(seg)

    def _activity_steady(self, w) -> str:
        """监控等待 / 空闲（含失败）：
        • 状态 <状态> │ 下一轮 <cd> │ 上一轮 <结果> │ 来源 Google。"""
        anchor = ui_meta("• 状态")
        if w and w.running:
            status = ui_ok("监控中")
        elif w and w.last_result and not w.last_result.ok:
            status = ui_error("抓取失败")
        else:
            status = ui_meta("未开始")
        seg = [f"{anchor}  {status}"]
        cd = "—"
        if w and w.countdown_s is not None:
            m, s = divmod(int(w.countdown_s), 60)
            cd = f"{m} 分 {s:02d} 秒"
        # 空占位（—）用灰色，与 gmail 中性态一致；有实值时用默认前景。
        nxt = ui_meta("—") if cd == "—" else ui_value(cd)
        seg.append(f"{ui_meta('下一轮')} {nxt}")
        if w and w.last_result:
            last = (self._last_round_markup()
                    if w.last_result.ok
                    else ui_error(w.last_result.error or "失败"))
        else:
            last = ui_meta("—")
        seg.append(f"{ui_meta('上一轮')} {last}")
        seg.append(self._gmail_footer())
        return " │ ".join(seg)

    def _render_runstate(self) -> None:
        """底部唯一的全局 activity 行：抓取中 / 监控等待 / 空闲 — 互斥，只留一行。"""
        w = self.watcher
        if w and w.current_round_started_at is not None:
            line = self._activity_fetching(w)
        else:
            line = self._activity_steady(w)
        self.query_one("#activity", Static).update(line)

    def _render_status_ticker(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        # 空闲页没有倒计时或进度可更新；避免无意义的重绘清掉 VS Code
        # 终端刚完成的鼠标选区。监控/抓取进行中才需要每秒刷新。
        w = self.watcher
        if not (w and (w.running or w.current_round_started_at)):
            return
        self._render_runstate()

    # ------------------------------------------------------------------
    # 键盘路由
    # ------------------------------------------------------------------
    def on_key(self, event: events.Key) -> None:
        # FieldEditor 本身也认识 Esc，但查找态的 Esc 语义是退出整个查找，
        # 不能先只取消输入、再要求用户按第二次 Esc。
        if self.editing.context == "search" and event.key == "escape":
            event.stop()
            self._leave_search()
            return
        if self.editing.context:
            # 任何行内编辑（settings / setup）统一走 FieldEditor
            self._edit_key(event)
            return
        k = event.key
        page = self.page
        # q = 退出（筛选页里 q 是搜索字符，用 Esc 返回后 q 或 Ctrl+C 退出）
        if page != "filters" and k == "q":
            event.stop()
            self.exit()
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
        # pi 式：输入即筛、↑↓ 移动、空格/回车 选中；Tab 切维度
        if k == "tab":
            event.stop()
            self._filters_dim_cycle()
        elif k == "up":
            event.stop()
            self._filters_move(-1)
        elif k == "down":
            event.stop()
            self._filters_move(1)
        elif k == "backspace":
            event.stop()
            self._filters_backspace()
        elif k == "escape":
            event.stop()
            if self.fv.query:
                self.fv.query = ""
                self.fv.index = 0
                self.fv.top = 0
                self._render_filters_list()
            else:
                self._leave_filters(commit=False)
        elif k in ("space", "enter"):
            event.stop()
            if k == "space":
                if self.fv.query:
                    # 允许多词搜索：输入态空格作为搜索内容
                    self._filters_type(" ")
                else:
                    self._filters_toggle()
            else:  # enter
                items = self._filters_items()
                if self.fv.query:
                    if items and 0 <= self.fv.index < len(items):
                        # 输入态回车：选中收窄后的当前项并清空输入，继续多选
                        self._filters_toggle()
                    elif self._dim_custom_allowed():
                        # 无匹配候选：直接把输入当作自定义条目加入当前维度
                        self._filters_add_custom(self.fv.query)
                    else:
                        # 课程类别不允许手动添加，仅可从自动探测的候选中选择
                        self.log_line(
                            f"课程类别不能手动添加，请从候选中选择：{self.fv.query}")
                    self.fv.query = ""
                    self.fv.index = 0
                    self.fv.top = 0
                    self._render_filters_list()
                else:
                    self._leave_filters(commit=True)
        else:
            # 其它可打印字符 → 输入即筛（含中文）。Textual 字母的 char 常为空，
            # 需回退到单字符的 key（如 'a'），否则用 char。
            char = getattr(event, "char", None)
            key = getattr(event, "key", "")
            text = char if char else (key if len(key) == 1 else None)
            if text and getattr(event, "is_printable", False):
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
            self._run_round()
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
            self.exit()
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
        elif k == "enter":
            event.stop()
            self._open_detail()

    def _move_cursor(self, step: int) -> None:
        rows = self._visible_rows()
        if not rows:
            return
        self.main.index = min(max(0, self.main.index + step), len(rows) - 1)
        avail = max(1, self.size.height - 19)
        if self.main.index < self.main.top:
            self.main.top = self.main.index
        if self.main.index >= self.main.top + avail:
            self.main.top = self.main.index - avail + 1
        self._render_course_window(rows, avail)

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
        if k == "down":
            event.stop()
            self._setup_edit_email()   # ↓ = 编辑收件邮箱（行内）
            return
        if k in ("enter", " "):
            event.stop()
            if not self.cfg.notify.to:
                self.log_line("请先填写收件邮箱")
                self._setup_edit_email()
                return
            self._finish_setup()
        elif k == "escape":
            event.stop()
            self.exit()  # 首启 Esc = 退出程序

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
            if f["kind"] == "enum":
                self._settings_cycle(f, 1)
            else:
                self._settings_edit(f)
        elif k == "t":
            event.stop()
            self._test_mail_draft()
        elif k == "ctrl+s":
            event.stop()
            self._settings_save()
        elif k == "escape":
            event.stop()
            self._leave_settings(commit=False)

    def _test_mail_draft(self) -> None:
        # 只使用当前 draft，不落盘
        to = self.sd.get("to", "").strip()
        gws_from = self.sd.get("gws_from", "").strip()
        if not to:
            self.log_line("✗ 收件邮箱为空，无法测试")
            return
        if not notifier.gws_available():
            self.log_line("✗ gws 未安装，无法发送")
            return
        n = self.cfg.notify
        self.log_line("正在发送测试邮件…（使用当前改动，尚未保存）")
        ok = notifier.send_email_with_retry(
            type(n)(to=to, gws_from=gws_from,
                    min_interval_min=n.min_interval_min,
                    max_per_hour=n.max_per_hour),
            "【courser】测试邮件",
            "courser 测试邮件：设置里的改动尚未保存，此测试不落盘。",
            log=self.log_line)
        self.log_line("测试邮件已发送" if ok else "✗ 测试邮件发送失败（已重试 3 次），请检查 gws / 网络")

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
            self.log_line(f"监控开始：每约 {self.cfg.interval_min} 分钟一轮（带抖动）")

    @work(thread=True, exclusive=True)
    def _run_round(self) -> None:
        w = self.watcher
        if w is None:
            return
        self.log_line("手动触发一轮抓取…")
        w.run_round()

    def _on_round(self, r: RoundResult) -> None:
        self.call_from_thread(self._apply_round, r)

    def _apply_round(self, r: RoundResult) -> None:
        # 本轮结束：清掉进行中的进度，避免残留"正在读取…"
        self.prog.clear()
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
        if self.watcher:
            self.watcher.stop()


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="courser",
                                 description="PKU 补退选空余名额监控（纯文本 TUI）")
    ap.add_argument("--once", action="store_true",
                    help="不启动 TUI，直接执行一轮抓取并打印结果（便于 cron/调试）")
    args = ap.parse_args(argv)

    load_env_file()
    cfg = Config.load()

    if args.once:
        import sys

        def cli_log(msg: str) -> None:
            print(f"[courser] {msg}", flush=True)

        def cli_progress(done, total, op):
            # 与 TUI 相同的抓取进度：单行实时更新到 stderr，不污染 stdout 的结果输出
            frac = f"{done}/{total}" if total else str(done)
            sys.stderr.write(f"\r[courser] 抓取 {frac} 步 · {op}" + " " * 4)
            sys.stderr.flush()

        sched = MonitorScheduler(cfg, log=cli_log, on_progress=cli_progress)
        try:
            r = sched.run_round()
        finally:
            # 清掉进度行，避免和最终结果混在一起
            sys.stderr.write("\r" + " " * 90 + "\r")
        seats = [c for c in r.matched if c.has_seats]
        print(f"结果：{'成功' if r.ok else '失败'} | 页数 {r.pages} | 课程 {r.total} | "
              f"命中 {len(r.matched)} | 其中空余 {len(seats)}")
        for c in r.matched:
            print(f"  - {c.name} [{c.course_no}] {c.category} {c.dept} "
                  f"限选/已选 {c.seats_raw} 空余 {c.avail} 状态 {c.status or '—'}")
        return 0 if r.ok else 2

    # mouse=False：不请求鼠标报告；终端负责拖拽选择/Cmd+C，TUI 保持备用屏。
    CourserApp(cfg).run(mouse=False)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
