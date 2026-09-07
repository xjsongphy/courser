"""courser 的纯文本监控 TUI。

定位：一个 **persistent status monitor**，而不是塞进终端的桌面应用。主页在
常态下只呈现当前状态、关注的课程与最近一次事件；所有配置都通过少量二级页面
完成。页面用线条边框面板分区，配色克制：

- 默认前景为正文，bold = 标题/区块/当前项，dim = 次要元信息；
- cyan 只表示可交互 / 当前值 / 当前光标；
- green = 成功 / 有空余 / 监控中；yellow = 警告（风控、未就绪）；red = 失败；
- 不用自造色与品牌色。全中文界面。

交互规范（一种操作一种入口，一个键一种语义）：
- Enter = 进入 / 确认；Esc = 返回 / 放弃（**Esc 任何地方都不保存**）；
- ↑↓ = 移动，Space = 开始/停止或选中/取消（各页内遵循）；
- 主页键：Space 开始/停止 · r 立即抓取 · f 筛选 · s 设置 · l 日志 ·
  1/2/3 视图（全部/符合筛选/只看空余）· h 帮助 · q 退出。
主页无输入框；仅筛选搜索、设置字段编辑、首启向导的填写需要底部单行输入。
"""

from __future__ import annotations

import argparse
import copy
import json
import shutil
import sys
import time
from pathlib import Path
from typing import Optional

from rich.cells import cell_len
from rich.markup import escape
from textual import events, on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Static

from . import notifier
from .config import Config, load_env_file
from .filters import FilterSet
from .fetch import Course
from .watcher import RoundResult, Watcher

GROUPS = [("names", "课程名"), ("categories", "课程类别"), ("depts", "开课院系")]

# 课程详情字段（中文标签）——含旧“查看最近一次结果”的全部信息
COURSE_DETAIL_FIELDS = [
    ("课程号", "course_no"), ("课程名", "name"), ("课程类别", "category"),
    ("学分", "credits"), ("周学时", "weekly_hours"), ("教师", "teacher"),
    ("课序号", "class_no"), ("开课单位", "dept"), ("年级", "grade"),
    ("时间地点", "schedule"), ("P/NP", "pnp"), ("限/选", "seats_raw"),
    ("选课状态", "status"), ("所属页", "page"), ("课程id", "seq"),
]

# 设置页字段分组：[组名, [字段…]]；字段 dict：key/label/kind
# kind: text|password|int|float|enum；enum 带 opts=[(value,label)]
SETTINGS_FIELDS: list[tuple[str, list[dict]]] = [
    ("账号凭据（可选；留空则依赖浏览器密码管理器自动填充）", [
        {"key": "username", "label": "学号", "kind": "text"},
        {"key": "password", "label": "密码", "kind": "password"},
    ]),
    ("邮件通知（gws 发送，需先 `gws auth login` 授权）", [
        {"key": "to", "label": "收件邮箱", "kind": "text"},
        {"key": "gws_from", "label": "gws 发件账号", "kind": "text"},
        {"key": "max_per_hour", "label": "每小时最多发送", "kind": "int"},
        {"key": "min_interval_min", "label": "同课通知冷却（分）", "kind": "float"},
    ]),
    ("轮询节奏（自动带随机抖动）", [
        {"key": "interval_min", "label": "轮询间隔（分）", "kind": "float"},
        {"key": "interval_jitter", "label": "间隔抖动", "kind": "float"},
        {"key": "page_delay_min", "label": "翻页间隔下限（秒）", "kind": "float"},
        {"key": "page_delay_max", "label": "翻页间隔上限（秒）", "kind": "float"},
    ]),
    ("行为", [
        {"key": "session", "label": "opencli 会话名", "kind": "text"},
        {"key": "window", "label": "浏览器窗口", "kind": "enum",
         "opts": [("background", "后台窗口（不抢焦点）"), ("foreground", "前台窗口")]},
        {"key": "force_relogin", "label": "每轮强制重新登录", "kind": "enum",
         "opts": [("false", "关"), ("true", "开")]},
    ]),
]

CSS = """
Screen { background: transparent; }
Vertical, VerticalScroll, Horizontal, Static, Input { background: transparent; }
VerticalScroll:focus { border: none; }
Input { border: none; padding: 0; }
Input:focus { border: none; }

.panel { border: solid #5c5c5c; padding: 0 1; }

#brand { height: 1; padding: 0 1; }
#stage { height: 1fr; padding: 0 1; }

/* 主页 */
#page-main { height: 1fr; padding: 0 1; }
#summary, #event { height: auto; }
#courselist { height: auto; }
#coursehead { height: auto; }

/* 次级整页面板 */
#page-filters, #page-settings, #page-logs, #page-help,
#page-detail, #page-setup { height: 1fr; padding: 0 1;
                            border: solid #5c5c5c; }

#logscroll, #helpscroll, #detscroll { height: 1fr; }
#filters_list, #settings_list, #setupbody { height: auto; }
#seditrow, #setupeditrow { height: 1; margin-top: 1; display: none; }
#seditrow Static, #setupeditrow Static { color: cyan; }
#seditrow Input, #setupeditrow Input { width: 1fr; }

#keys, #runstate { height: 1; padding: 0 1; }
"""


class FocusScroll(VerticalScroll):
    """可聚焦滚动区：↑↓/PgUp/PgDn/Home/End 原生滚动，字母数字键交给应用。"""

    can_focus = True


class FocusableStatic(Static):
    """惰性焦点锚：可被聚焦但自己不消费任何键，让方向键/字母键冒泡到应用。"""

    can_focus = True


def _cut(s: str, width: int) -> str:
    """按显示宽度截断（中文按两格计），超宽以 … 结尾。"""
    if cell_len(s) <= width:
        return s
    out = ""
    for ch in s:
        if cell_len(out + ch) + 1 > width:
            break
        out += ch
    return out + "…"


def _pad(s: str, width: int) -> str:
    s = _cut(s, width)
    return s + " " * (width - cell_len(s))


def _field_value(cfg: Config, key: str) -> str:
    if key == "username":
        return cfg.credentials.username
    if key == "password":
        return cfg.credentials.password
    if key == "to":
        return cfg.notify.to
    if key == "gws_from":
        return cfg.notify.gws_from
    if key == "max_per_hour":
        return str(cfg.notify.max_per_hour)
    if key == "min_interval_min":
        return str(cfg.notify.min_interval_min)
    if key == "force_relogin":
        return "true" if cfg.force_relogin else "false"
    return str(getattr(cfg, key, ""))


def _field_mutate(cfg: Config, key: str, value: str) -> None:
    if key == "username":
        cfg.credentials.username = value
    elif key == "password":
        cfg.credentials.password = value
    elif key == "to":
        cfg.notify.to = value
    elif key == "gws_from":
        cfg.notify.gws_from = value
    elif key == "max_per_hour":
        cfg.notify.max_per_hour = max(1, int(float(value)))
    elif key == "min_interval_min":
        cfg.notify.min_interval_min = float(value)
    elif key == "force_relogin":
        cfg.force_relogin = value == "true"
    elif key == "session":
        cfg.session = value
    elif key == "window":
        cfg.window = value
    else:
        setattr(cfg, key, float(value))


def _risk_text(percent: int, label: str) -> str:
    color = {"高": "red", "极高": "red", "中": "yellow",
             "已触发/疑似": "red"}.get(label, "yellow")
    if label == "无" or percent == 0:
        return ""
    return f"风控 [bold {color}]⚠ {percent}%({label})[/]"


class CourserApp(App):
    """纯文本菜单 TUI：主页面 + 筛选/设置/日志/帮助/详情/首启设置。"""

    TITLE = "courser"
    SUB_TITLE = "PKU 补退选空余名额监控"
    CSS = CSS
    BINDINGS = [Binding("ctrl+c", "quit", "退出", show=False, priority=True)]

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.page = "main"
        self.log_buf: list[str] = []
        self.courses: list[Course] = []
        self.snapshot_meta = ""
        self.snapshot_ts: Optional[str] = None
        self.candidate_lists = {"names": [], "categories": [], "depts": []}
        self.watcher: Optional[Watcher] = None
        # 主页面课程视图 & 光标
        self.view = "all"
        self.c_idx = 0
        self.c_top = 0
        # 抓取进度（本轮步骤/总数/当前操作）
        self._prog_done: Optional[int] = None
        self._prog_total: Optional[int] = None
        self._prog_op = ""
        # 筛选页状态
        self.f_dim = 0
        self.f_idx = 0
        self.f_top = 0
        self.f_query = ""              # 顶部输入即筛
        self.auto_gather = True        # 无最近结果时进入筛选页自动抓一轮生成候选
        self._gathering = False
        # settings draft 与行索引
        self.sd: dict[str, str] = {}
        self.s_rows: list[tuple[str, dict]] = []
        self.s_idx = 0
        # 单行输入编辑态
        self._editing: Optional[str] = None   # None / settings / setup / filters-commit?
        self._editing_key: Optional[str] = None
        self._editing_label = ""
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
        for p in (Path("data/last_round.json"), Path("data/courses_snapshot.json")):
            if p.exists():
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    self.courses = [Course(**{k: v for k, v in c.items()})
                                    for c in d.get("courses", [])]
                    self.snapshot_ts = d.get("ts")
                    pages, n = d.get("pages"), d.get("n")
                    self.snapshot_meta = (f"{pages} 页 · {n} 门课程"
                                          if pages is not None else "")
                    du = d.get("distinct")
                    if du:
                        self.candidate_lists = {k: list(v) for k, v in du.items()}
                    break
                except Exception:
                    continue

    def _save_snapshot(self, r: RoundResult) -> None:
        distinct = {
            "names": sorted({c.name for c in r.courses if c.name}),
            "categories": sorted({c.category for c in r.courses if c.category}),
            "depts": sorted({c.dept for c in r.courses if c.dept}),
        }
        self.candidate_lists = distinct
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        self.snapshot_ts = ts
        self.snapshot_meta = f"{r.pages} 页 · {len(r.courses)} 门课程"
        d = {"ts": ts, "pages": r.pages, "n": len(r.courses), "distinct": distinct,
             "courses": [c.__dict__ for c in r.courses]}
        Path("data").mkdir(exist_ok=True)
        Path("data/last_round.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # ------------------------------------------------------------------
    # 日志（环形缓冲；落盘由 watcher 负责）
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
    # 状态文本
    # ------------------------------------------------------------------
    def _last_round_text(self) -> str:
        r = self.watcher.last_result if self.watcher else None
        if r is None:
            return "—"
        if r.ok:
            return f"{r.pages}页/{r.total}课 {r.duration_s:.0f}s [green]成功[/]"
        return f"[red]失败[/] {escape(r.error or '')}"

    def _mail_text(self) -> str:
        if not notifier.gws_available():
            return "gws [yellow]未安装[/]"
        if not self.cfg.notify.to:
            return "收件邮箱 [yellow]未填写[/]"
        return "[green]✓ 已就绪[/]"

    def _monitor_summary(self) -> list[str]:
        w = self.watcher
        lines = []
        if w and w.running:
            cd = "--"
            if w.countdown_s is not None:
                m, s = divmod(w.countdown_s, 60)
                cd = f"[cyan]{m} 分 {s} 秒[/]"
            lines.append(f"[bold green]● 监控中[/]        下一轮 {cd}")
        else:
            lines.append("[dim]未开始[/]            [dim]按空格开始监控[/]")
        lines.append(f"上一轮      {self._last_round_text()}")
        fs = self.cfg.filters
        if fs.empty:
            lines.append("筛选        [dim]未配置（不会告警）· 按 f 配置[/]")
        else:
            gs = " · ".join(f"{n}×{len(v)}" for n, v in fs.active_groups)
            mode = "任一" if fs.match != "all" else "全部"
            lines.append(f"筛选        [cyan]{gs}[/] · 满足{mode}条件")
        lines.append(f"通知        {self._mail_text()}")
        if w and w.last_result:
            risk = _risk_text(w.last_result.risk_percent, w.last_result.risk_label)
            if risk:
                lines.append(risk)
        return lines

    def _event_line(self) -> str:
        if not self.log_buf:
            return ""
        raw = self.log_buf[-1]
        body = raw.split("  ", 1)[-1] if "  " in raw else raw
        if "✗" in body or "失败" in body or "异常" in body:
            color = "red"
        elif "⚠" in body or "风控" in body:
            color = "yellow"
        elif "已发送" in body or "成功" in body or "完成" in body or "✓" in body:
            color = "green"
        else:
            color = "default"
        return f"[{color}]{escape(body)}[/]"

    # ------------------------------------------------------------------
    # 课程行 / 自适应列
    # ------------------------------------------------------------------
    def _visible_rows(self) -> list[Course]:
        fs = FilterSet(self.cfg.filters)
        rows = self.courses
        if self.view == "matched":
            rows = [c for c in rows if fs.matches(c)]
        elif self.view == "seats":
            rows = [c for c in rows if c.has_seats]
        return rows

    def _columns(self, W: int) -> list[tuple[str, str, int]]:
        """按可用宽度 W 贪心选列：页/课程号/限选/空余必保，其余依次挤入，
        课程名列吸收剩余宽度 —— 窄屏自动缩列、只截断课程名，宽屏补全字段。"""
        pre = [("page", "页", 3), ("no", "课程号", 10)]
        fixed = [("seats", "限/选", 7), ("avail", "空余", 6)]
        opt = [("cat", "课程类别", 20), ("dept", "开课单位", 16),
               ("teacher", "教师", 12), ("status", "状态", 8)]
        # 已占：必保列 + 每列间 1 空格 + 左侧光标区 3
        used = sum(w for _k, _l, w in pre + fixed) + \
            (len(pre) + len(fixed) - 1) + 3
        budget = max(6, W - used)
        picked: list[tuple[str, str, int]] = []
        for key, label, width in opt:
            if width + 1 <= budget:
                picked.append((key, label, width))
                budget -= width + 1
        name_w = budget
        cols: list[tuple[str, str, int]] = []
        for k, l, w in pre:
            cols.append((k, l, w))
        cols.append(("name", "课程", name_w))
        cols.extend(picked)
        cols.extend(fixed)
        return cols

    def _header_labels(self, cols) -> str:
        cells = [_pad(l, w) for _k, l, w in cols]
        return f"[bold]{' '.join(cells)}[/]"

    def _row_body(self, c: Course, matched: bool, cols) -> str:
        cells: list[str] = []
        for key, _l, w in cols:
            if key == "page":
                cells.append(_pad(str(c.page) if c.page else "—", w))
            elif key == "no":
                cells.append(_pad(escape(c.course_no or ""), w))
            elif key == "name":
                body_w = max(1, w - (4 if matched else 0))  # 为 ★ 前缀留位
                name = _pad(escape(c.name or ""), body_w)
                if matched:
                    name = f"[cyan]★[/] {name}"
                cells.append(name)
            elif key == "cat":
                cells.append(_pad(escape(c.category or ""), w))
            elif key == "dept":
                cells.append(_pad(escape(c.dept or ""), w))
            elif key == "teacher":
                cells.append(_pad(escape(c.teacher or ""), w))
            elif key == "seats":
                cells.append(_pad(c.seats_raw or
                                  (f"{c.selected}/{c.quota}"
                                   if c.quota is not None else "—"), w))
            elif key == "avail":
                avail = "—" if c.avail < 0 else str(c.avail)
                st = "green" if c.has_seats else "dim"
                cells.append(f"[{st}]{_pad(avail, w)}[/]")
            elif key == "status":
                cells.append(f"[dim]{_pad(escape(c.status or ''), w)}[/]")
        return " ".join(cells).rstrip()

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
                yield Static("", id="progress", markup=True)
                yield Static("", id="courselist", markup=True)
                yield Static("", id="event", markup=True)
            # 筛选（pi 式：顶部输入即筛 + 列表，↑↓ 选，空格/回车 切换）
            with Vertical(id="page-filters"):
                yield Static("", id="filters_hdr", markup=True)
                yield Static("", id="fsearch", markup=True)
                yield Static("", id="filters_list", markup=True)
            # 设置
            with Vertical(id="page-settings"):
                yield Static("", id="settings_hdr", markup=True)
                yield Static("", id="settings_list", markup=True)
                with Horizontal(id="seditrow"):
                    yield Static("", id="sedit_label", classes="inlabel")
                    yield Input(id="sedit_input")
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
            # 首次设置
            with Vertical(id="page-setup"):
                yield Static("", id="setupbody", markup=True)
                with Horizontal(id="setupeditrow"):
                    yield Static("", id="setupedit_label", classes="inlabel")
                    yield Input(id="setup_input")
        yield Static("", id="keys")
        yield Static("", id="runstate")

    def on_mount(self) -> None:
        self.watcher = Watcher(self.cfg, log=self._thread_log, on_round=self._on_round,
                               on_progress=self._thread_progress)
        self._reset_runstate_interval()
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
        "main": "[cyan]空格[/] 开始/停止 · [cyan]r[/] 立即抓取 · "
                "[cyan]1/2/3[/] 全部/筛选/空余 · [cyan]f[/] 筛选 · "
                "[cyan]s[/] 设置 · [cyan]l[/] 日志 · [cyan]h[/] 帮助 · "
                "[cyan]q[/] 退出",
        "filters": "[cyan]输入过滤[/] · [cyan]↑↓[/] 选择 · [cyan]空格[/] 选中/取消 · "
                   "[cyan]回车[/] 确认 · [cyan]Tab[/] 维度 · "
                   "[cyan]Esc[/] 放弃并返回",
        "settings": "[cyan]↑↓[/] 选择 · [cyan]←/→[/] 切换选项 · [cyan]回车[/] 编辑 · "
                    "[cyan]t[/] 测试邮件 · [cyan]ctrl+s[/] 保存 · [cyan]Esc[/] 放弃",
        "logs": "[cyan]↑↓[/] 滚动 · [cyan]Esc[/] 返回主页",
        "help": "[cyan]↑↓[/] 滚动 · [cyan]Esc[/] 返回主页",
        "detail": "[cyan]Esc[/] 返回主页",
        "setup": "[cyan]↓[/] 编辑收件邮箱 · [cyan]回车[/] 继续 · "
                 "[cyan]Esc[/] 退出程序",
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
        self.query_one("#keys", Static).update(self.HINTS[page])
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
        self._editing = None
        self._editing_key = None
        self.query_one("#seditrow", Horizontal).display = False
        self.query_one("#setupeditrow", Horizontal).display = False
        # 焦点还给惰性锚，避免停在隐藏输入行上吞键
        self._anchor_focus()

    def _render_main(self, force: bool = False) -> None:
        if not force and self.page != "main":
            return
        self.query_one("#brand", Static).update(
            "[bold]courser[/][dim] — PKU 补退选空余名额监控[/]")
        W = max(40, self.size.width - 4)
        # 标题栏：视图选择 + 快照元信息
        meta = ""
        if self.snapshot_ts:
            meta = f"[dim]最近一次：{escape(self.snapshot_ts)}"
            if self.snapshot_meta:
                meta += f" · {escape(self.snapshot_meta)}"
            meta += "[/]"
        head = "课程   "
        head += " ".join(
            [("[bold cyan]1 全部[/]" if self.view == "all" else "[dim]1 全部[/]"),
             ("[bold cyan]2 符合筛选[/]" if self.view == "matched" else "[dim]2 符合筛选[/]"),
             ("[bold cyan]3 只看空余[/]" if self.view == "seats" else "[dim]3 只看空余[/]")])
        if meta:
            head += f"   {meta}"
        self.query_one("#coursehead", Static).update(head)
        summary = "\n".join(self._monitor_summary())
        self.query_one("#summary", Static).update(summary)

        rows = self._visible_rows()
        avail_h = max(1, self.size.height - 18)
        self._render_course_window(rows, avail_h)
        ev = self._event_line()
        self.query_one("#event", Static).update(ev if ev else "")

    def _render_main_lite(self) -> None:
        """主页的轻量刷新（事件/运行状态行），不做整页重排。"""
        if self.page != "main":
            return
        try:
            ev = self._event_line()
            self.query_one("#event", Static).update(ev if ev else "")
        except Exception:
            pass
        self._render_runstate()

    def _render_course_window(self, rows: list[Course], avail: int) -> None:
        body = self.query_one("#courselist", Static)
        if not rows:
            body.update(
                "[dim]还没有可显示的结果。按 r 立即抓取，或按空格开始监控。[/]"
                if not self.courses else
                "[dim]（当前视图无课程：按 1 查看全部 / 按 2 查看符合筛选的课程）[/]")
            return
        if self.c_idx >= len(rows):
            self.c_idx = len(rows) - 1
        if self.c_idx < self.c_top:
            self.c_top = self.c_idx
        if self.c_idx >= self.c_top + avail:
            self.c_top = self.c_idx - avail + 1
        W = max(40, self.size.width - 4)
        cols = self._columns(W)
        fs = FilterSet(self.cfg.filters)
        lines = []
        lines.append(self._header_labels(cols))
        # 名称列已并入第一段；这里给出行
        for i in range(self.c_top, min(len(rows), self.c_top + avail)):
            c = rows[i]
            matched = fs.matches(c)
            lines.append(self._course_line(c, matched, cols,
                                           cursor=i == self.c_idx))
        body.update("\n".join(lines))

    def _course_line(self, c: Course, matched: bool, cols, cursor: bool) -> str:
        cur = "[cyan]❯[/] " if cursor else "   "
        return cur + self._row_body(c, matched, cols)

    # ------------------------------------------------------------------
    # 渲染：筛选页
    # ------------------------------------------------------------------
    def _begin_filters(self) -> None:
        self._f_backup = copy.deepcopy(self.cfg.filters)  # Esc 放弃依据
        self.f_dim = 0
        self.f_query = ""
        self.f_idx = 0
        self.f_top = 0
        self._render_filters_list()
        self._ensure_candidates()

    def _ensure_candidates(self) -> None:
        # 候选来自最近一次抓取结果（candidate_lists）。若一个结果都没有，
        # 自动抓一轮来生成候选，避免用户面对空列表无从选择。
        if not self.auto_gather or self._gathering or self.courses:
            return
        if self.watcher and self.watcher.running:
            return
        self._gathering = True
        self.log_line("暂无最近抓取结果，自动抓取一轮以生成候选…")
        self._render_filters_list()
        self._run_round()

    def _on_gather_done(self) -> None:
        self._gathering = False
        if self.page == "filters":
            self._render_filters_list()

    def _filters_entries(self) -> list[str]:
        return list(getattr(self.cfg.filters, GROUPS[self.f_dim][0]))

    def _filters_dim_cycle(self) -> None:
        self.f_dim = (self.f_dim + 1) % len(GROUPS)
        self.f_idx = 0
        self.f_top = 0
        self._render_filters_list()

    def _filters_items(self) -> list[str]:
        """当前列表 = 快照候选（按输入即筛的查询过滤）+ 已选但不在候选里的。"""
        gid = GROUPS[self.f_dim][0]
        q = self.f_query.lower()
        seen = set(self.candidate_lists[gid])
        items = [e for e in self.candidate_lists[gid] if (not q or q in e.lower())]
        for e in getattr(self.cfg.filters, gid):
            if e not in seen and (not q or q in e.lower()):
                items.append(e)
        return items

    def _render_filters_list(self) -> None:
        dim_name = GROUPS[self.f_dim][1]
        entries = set(self._filters_entries())
        mode = "满足任一条件" if self.cfg.filters.match != "all" else "满足全部条件"
        self.query_one("#filters_hdr", Static).update(
            f"[bold]筛选配置[/]   维度 [cyan]Tab[/]：{dim_name}"
            f"（已选 {len(entries)}）· 组合：[cyan]{mode}[/]")
        # pi 式输入即筛行
        fs = self.query_one("#fsearch", Static)
        if self._gathering:
            fs.update("[dim]正在抓取候选…（完成后会自动列出课程名 / 课程类别 / 开课院系）[/]")
        else:
            q = escape(self.f_query)
            hint = ("（输入文字可过滤下方列表）" if not q else "")
            fs.update(f"[cyan]❯ 搜索：[/]{q}[cyan]▍[/]"
                      f"[dim] {hint}[/]".rstrip())
        items = self._filters_items()
        listw = self.query_one("#filters_list", Static)
        if not items:
            if self._gathering:
                listw.update("")
                return
            if self.f_query:
                listw.update(
                    f"[dim]没有匹配「{escape(self.f_query)}」的条目。"
                    f"按回车可把它添加为当前维度的筛选条件。[/]")
            else:
                listw.update(
                    "[dim]本维度暂无可选条目。\n"
                    "  候选取自最近一次抓取结果；若还没有抓取结果，进入本页时会自动抓一轮。\n"
                    "  仍无条目时，可在搜索框输入后按回车，将其添加为筛选条件。[/]")
            return
        if self.f_idx >= len(items):
            self.f_idx = len(items) - 1
        if self.f_idx < self.f_top:
            self.f_top = self.f_idx
        maxlines = max(3, min(20, self.size.height - 12))
        if self.f_idx >= self.f_top + maxlines:
            self.f_top = self.f_idx - maxlines + 1
        window = items[self.f_top:self.f_top + maxlines]
        cands = set(self.candidate_lists[GROUPS[self.f_dim][0]])
        out = []
        for i, it in enumerate(window):
            idx = self.f_top + i
            cur = "[cyan]❯[/]" if idx == self.f_idx else " "
            mark = "[cyan]✓[/]" if it in entries else " "
            extra = " [dim](手动添加)[/]" if it not in cands else ""
            out.append(f"{cur} {mark} {escape(it)}{extra}")
        listw.update("\n".join(out))

    def _filters_move(self, step: int) -> None:
        items = self._filters_items()
        if not items:
            return
        self.f_idx = min(max(0, self.f_idx + step), len(items) - 1)
        self._render_filters_list()

    def _filters_toggle(self) -> None:
        items = self._filters_items()
        if not (0 <= self.f_idx < len(items)):
            return
        gid = GROUPS[self.f_dim][0]
        lst = getattr(self.cfg.filters, gid)
        value = items[self.f_idx]
        if value in lst:
            lst.remove(value)
            self.log_line(f"已取消筛选：{value}")
        else:
            lst.append(value)
            self.log_line(f"已添加筛选：{value}")
        self._render_filters_list()

    def _filters_type(self, char: str) -> None:
        self.f_query += char
        self.f_idx = 0
        self.f_top = 0
        self._render_filters_list()

    def _filters_backspace(self) -> None:
        if not self.f_query:
            return
        self.f_query = self.f_query[:-1]
        self.f_idx = 0
        self.f_top = 0
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
        self.s_idx = 0
        self._render_settings_list()
        self.set_focus(None)

    def _render_settings_list(self) -> None:
        hdr = self.query_one("#settings_hdr", Static)
        gws = "已安装" if notifier.gws_available() else "[yellow]未找到 gws[/]"
        hdr.update(f"[bold]设置[/]  gws：{gws}   "
                   "[dim]ctrl+s 保存 · t 测试邮件（不保存）[/]")
        out = []
        last = None
        for i, (gname, f) in enumerate(self.s_rows):
            if gname != last:
                out.append(f"[bold]{escape(gname)}[/]")
                last = gname
            cur = "[cyan]❯[/]" if i == self.s_idx else " "
            val = self._draft_display(f)
            if i == self.s_idx:
                out.append(f"{cur} {escape(f['label'])}：[cyan]{val}[/]")
            else:
                out.append(f"{cur} {escape(f['label'])}：[dim]{val}[/]")
        self.query_one("#settings_list", Static).update("\n".join(out))

    def _settings_move(self, step: int) -> None:
        n = len(self.s_rows)
        self.s_idx = min(max(0, self.s_idx + step), n - 1)
        self._render_settings_list()

    def _settings_edit(self, f: dict) -> None:
        self._editing = "settings"
        self._editing_key = f["key"]
        self._editing_label = f["label"]
        lab = self.query_one("#sedit_label", Static)
        inp = self.query_one("#sedit_input", Input)
        lab.update(f"✎ {escape(f['label'])}：")
        inp.password = f["kind"] == "password"
        inp.value = "" if f["kind"] == "password" else self.sd.get(f["key"], "")
        self.query_one("#seditrow", Horizontal).display = True
        inp.focus()

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

    @on(Input.Submitted, "#sedit_input")
    def _on_sedit_submit(self, event: Input.Submitted) -> None:
        key = self._editing_key
        val = event.value
        self._clear_editing()
        if key:
            self.sd[key] = val
        self._render_settings_list()
        # 不在此处保存；Esc/ctrl+s 决定

    # ------------------------------------------------------------------
    # 渲染：日志页 / 帮助 / 详情 / 首启设置
    # ------------------------------------------------------------------
    def _render_log(self) -> None:
        body = self.query_one("#logbody", Static)
        if not self.log_buf:
            body.update("[dim]（暂无日志；开始监控或抓取后这里会记录每一轮过程）[/]")
            return
        body.update("\n".join(escape(l) for l in self.log_buf[-300:]))

    def _render_help(self) -> None:
        self.query_one("#helpbody", Static).update(
            "[bold]courser — PKU 补退选空余名额监控[/]\n\n"
            "[bold]主页操作（纯键盘）[/]\n"
            "  空格  开始 / 停止监控      r   立即抓取一轮\n"
            "  1     全部课程            2   只看符合筛选\n"
            "  3     只看有空余           ↑↓   浏览课程（回车看详情）\n"
            "  f 筛选   s 设置   l 日志   h 帮助   q 退出\n\n"
            "[bold]监控流程（每轮重新登录）[/]\n"
            "  登出 → IAAA 登录（自动填充或设置内填学号/密码）→ 补退选\n"
            "  → 动态翻页读限/选 → 命中且空余经 gws 发邮件；人类节奏、绝不输验证码。\n\n"
            "[bold]筛选（f）[/]\n"
            "  课程名 / 课程类别 / 开课院系三维度；直接输入即可过滤（支持中文），\n"
            "  ↑↓ 选择，空格 选中/取消；有匹配时回车选中该项，无匹配时回车把它作为\n"
            "  自定义条目加入；Tab 切维度；在空搜索框回车保存，Esc 放弃\n"
            "  （改动未保存前均不落盘）。主页课程列表用 ★ 标记符合筛选的课程。\n\n"
            "[bold]设置（s）[/]\n"
            "  账号、邮件通知、轮询节奏、行为；t 测试邮件（只用当前改动，不保存）；\n"
            "  ctrl+s 保存，Esc 放弃。轮询间隔在「轮询节奏」里改。\n\n"
            "[bold]日志（l）[/]\n"
            "  完整运行过程记录，不占主页；主页只保留最近一条事件。\n\n"
            "[bold]安全与节奏[/]\n"
            "  轮询带随机抖动；检测到页面风控/警告语会放慢节奏并提示；\n"
            "  出错（登录失败/验证码）自动降速，不硬顶。\n\n[dim]Esc 返回[/]")

    def _render_detail(self) -> None:
        c = self._detail_course
        if c is None:
            self._show("main", force=True)
            return
        lines = [f"[bold]{escape(c.name or '（无名称）')}[/]\n"]
        avail = "—" if c.avail < 0 else str(c.avail)
        lines.append(f"空余：[{'green' if c.has_seats else 'dim'}]{avail}[/]"
                     f"[dim]  ★=符合当前筛选[/]\n")
        for label, attr in COURSE_DETAIL_FIELDS:
            if attr == "avail":
                continue
            raw = getattr(c, attr, "")
            if attr == "course_no":
                pass
            val = str(raw) if raw not in (None, "") else "—"
            if attr == "page" and not c.page:
                val = "—"
            lines.append(f"[dim]{_pad(label, 6)}[/] {escape(val)}")
        lines.append(f"[dim]{_pad('空余数', 6)}[/] "
                     f"[{'green' if c.has_seats else 'dim'}]{avail}[/]")
        lines.append("\n[dim]按 Esc 返回主页[/]")
        self.query_one("#detbody", Static).update("\n".join(lines))

    def _render_setup(self) -> None:
        body = self.query_one("#setupbody", Static)
        gws = notifier.gws_available()
        has_recip = bool(self.cfg.notify.to)
        lines = ["[bold]首次使用 — 配置课程监控[/]\n", "运行环境："]
        opencli = shutil.which("opencli")
        lines.append("  " + ("[green]✓[/] opencli 已安装"
                             if opencli else "[red]✗[/] opencli 未安装（请先安装并运行 opencli doctor）"))
        lines.append("  " + ("[green]✓[/] gws 已安装"
                             if gws else "[red]✗[/] gws 未安装（brew install gws）"))
        if gws:
            lines.append("  [yellow]⚠[/] gws 尚未授权——请先在命令行执行 gws auth login")
        lines.append("")
        lines.append("[bold]收件邮箱[/]（用于接收提醒，必填）")
        if has_recip:
            lines.append("  [green]✓[/] " + escape(self.cfg.notify.to))
        else:
            lines.append("  [yellow]未填写[/]")
        lines.append("")
        lines.append("学号 / 密码可留空：登录时由浏览器密码管理器自动填充。")
        lines.append("也可稍后在主页按 s，在「设置 → 账号凭据」中补充。")
        lines.append("")
        if has_recip:
            lines.append("[green]收件邮箱已配置，可以开始使用。[/]")
        else:
            lines.append("[yellow]请先填写收件邮箱。[/]")
        body.update("\n".join(lines))

    # ------------------------------------------------------------------
    # 首启设置：把“填邮箱”做成可编辑行，取代“按 2 我完成”
    # ------------------------------------------------------------------
    def _setup_edit_email(self) -> None:
        self._editing = "setup"
        self._editing_key = "to"
        lab = self.query_one("#setupedit_label", Static)
        inp = self.query_one("#setup_input", Input)
        lab.update("✎ 收件邮箱：")
        inp.password = False
        inp.value = self.cfg.notify.to
        self.query_one("#setupeditrow", Horizontal).display = True
        inp.focus()

    @on(Input.Submitted, "#setup_input")
    def _on_setup_submit(self, event: Input.Submitted) -> None:
        self.cfg.notify.to = event.value.strip()
        self._clear_editing()
        self._render_setup()
        if self.cfg.notify.to:
            self.log_line("收件邮箱已填写，回车开始使用")

    # ------------------------------------------------------------------
    # 运行状态行（屏幕底部）
    # ------------------------------------------------------------------
    def _reset_runstate_interval(self) -> None:
        pass

    # ------------------------------------------------------------------
    # 抓取进度（本轮步骤 + 当前操作实时提示）
    # ------------------------------------------------------------------
    def _thread_progress(self, done, total, op):
        self.call_from_thread(self._set_progress, done, total, op)

    def _set_progress(self, done, total, op):
        self._prog_done, self._prog_total, self._prog_op = done, total, op
        try:
            self._render_progress()
        except Exception:
            pass

    def _render_progress(self) -> None:
        if self.page != "main":
            return
        try:
            el = self.query_one("#progress", Static)
        except Exception:
            return
        done, total, op = self._prog_done, self._prog_total, self._prog_op
        if done is None:
            el.update("")
            return
        W = max(8, min(28, max(10, self.size.width - 64)))
        bar = ""
        if total:
            filled = int(W * max(0, min(1.0, done / total)))
            bar = "[cyan]" + "█" * filled + "[/][dim]" + "░" * (W - filled) + "[/] "
        frac = f"{done}/{total}" if total else str(done)
        el.update(f"[bold]抓取 {frac} 步[/] {bar}[dim]· 当前：{escape(op)}[/]")

    def _render_runstate(self) -> None:
        w = self.watcher
        run = "[bold green]● 监控中[/]" if (w and w.running) else "[dim]未开始[/]"
        cd = "--"
        if w and w.running and w.countdown_s is not None:
            m, s = divmod(w.countdown_s, 60)
            cd = f"[cyan]{m}分{s:02d}秒[/]"
        last = self._last_round_text()
        self.query_one("#runstate", Static).update(
            f"{run} · 下一轮 {cd} · 上一轮 {last}")

    def _render_status_ticker(self) -> None:
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        self._render_runstate()
        if self.page == "main":
            try:
                self._render_progress()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 键盘路由
    # ------------------------------------------------------------------
    def on_key(self, event: events.Key) -> None:
        if self._editing:
            if event.key == "escape":
                # 放弃当前字段编辑（不退出页面、不保存）
                self._clear_editing()
                if self.page == "settings":
                    self._render_settings_list()
                elif self.page == "setup":
                    self._render_setup()
                elif self.page == "filters":
                    self._render_filters_list()
                event.stop()
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
            if self.f_query:
                self.f_query = ""
                self.f_idx = 0
                self.f_top = 0
                self._render_filters_list()
            else:
                self._leave_filters(commit=False)
        elif k in ("space", "enter"):
            event.stop()
            if k == "space":
                if self.f_query:
                    # 允许多词搜索：输入态空格作为搜索内容
                    self._filters_type(" ")
                else:
                    self._filters_toggle()
            else:  # enter
                items = self._filters_items()
                if self.f_query:
                    if items and 0 <= self.f_idx < len(items):
                        # 输入态回车：选中收窄后的当前项并清空输入，继续多选
                        self._filters_toggle()
                    else:
                        # 无匹配候选：直接把输入当作自定义条目加入当前维度
                        self._filters_add_custom(self.f_query)
                    self.f_query = ""
                    self.f_idx = 0
                    self.f_top = 0
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
        gid = GROUPS[self.f_dim][0]
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
        self.c_idx = min(max(0, self.c_idx + step), len(rows) - 1)
        avail = max(1, self.size.height - 18)
        if self.c_idx < self.c_top:
            self.c_top = self.c_idx
        if self.c_idx >= self.c_top + avail:
            self.c_top = self.c_idx - avail + 1
        self._render_course_window(rows, avail)

    def _open_detail(self) -> None:
        rows = self._visible_rows()
        if not rows or not (0 <= self.c_idx < len(rows)):
            return
        self._detail_course = rows[self.c_idx]
        self._show("detail")

    def _set_view(self, v: str) -> None:
        if self.view != v:
            self.view = v
            self.c_idx = 0
            self.c_top = 0
            self._render_main(force=True)

    def _setup_key(self, k: str, event: events.Key) -> None:
        if k == "down":
            event.stop()
            self._setup_edit_email() if not self.cfg.notify.to else None
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
            self.exit()  # 审阅：首启 Esc = 退出程序

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
            _g, f = self.s_rows[self.s_idx]
            if f["kind"] == "enum":
                event.stop()
                self._settings_cycle(f, -1 if k == "left" else 1)
        elif k in ("enter",):
            _g, f = self.s_rows[self.s_idx]
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
        ok = notifier.send_email(type(n)(to=to, gws_from=gws_from,
                                         min_interval_min=n.min_interval_min,
                                         max_per_hour=n.max_per_hour),
                                 "【courser】测试邮件",
                                 "courser 测试邮件：设置里的改动尚未保存，此测试不落盘。",
                                 log=self.log_line)
        self.log_line("测试邮件已发送" if ok else "✗ 测试邮件发送失败，请检查 gws")

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
        self._prog_done = self._prog_total = None
        self._prog_op = "本轮完成"
        if r.ok and r.courses:
            self.courses = r.courses
            self._save_snapshot(r)
        if self._gathering:
            self._on_gather_done()
        if self.page == "main":
            self._render_main(force=True)
        elif self.page == "filters":
            self._render_filters_list()
        self._render_runstate()
        try:
            self._render_progress()
        except Exception:
            pass
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
        def cli_log(msg: str) -> None:
            print(f"[courser] {msg}", flush=True)

        w = Watcher(cfg, log=cli_log)
        r = w.run_round()
        seats = [c for c in r.matched if c.has_seats]
        print(f"结果：{'成功' if r.ok else '失败'} | 页数 {r.pages} | 课程 {r.total} | "
              f"命中 {len(r.matched)} | 其中空余 {len(seats)}")
        for c in r.matched:
            print(f"  - {c.name} [{c.course_no}] {c.category} {c.dept} "
                  f"限/选 {c.seats_raw} 空余 {c.avail} 状态 {c.status or '—'}")
        return 0 if r.ok else 2

    # mouse=False：纯键盘菜单界面（无主页输入框、无悬浮窗口）。
    CourserApp(cfg).run(mouse=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
