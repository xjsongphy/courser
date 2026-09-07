"""courser 的 Textual TUI。

界面刻意保持为终端原生的、类似 codex 的工作流：纯键盘操作（不启用
鼠标），一个简短状态行、课程结果与运行记录，以及底部命令输入框。
所有操作既有快捷键，也可通过 composer 输入 ``/start``、``/fetch``、
``/filters`` 等命令完成。配色遵循 codex 的 styles.md：默认前景色为
主，标题加粗、次要信息 dim；cyan 用于输入提示/状态，green/red 表示
成功/错误，magenta 为品牌色。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import events
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (DataTable, Input, Label,
                             ListItem, ListView, RichLog, Static)

from . import notifier
from .config import Config, load_env_file
from .filters import FilterSet
from .fetch import Course
from .watcher import RoundResult, Watcher

GROUPS = [("names", "课程名"), ("categories", "课程类别"), ("depts", "开课院系")]

TABLE_LAYOUTS = {
    "wide": [
        ("page", "页", 4), ("no", "课程号", 10), ("name", "课程名", 26),
        ("cat", "课程类别", 20), ("dept", "开课单位", 14), ("teacher", "教师", 14),
        ("seats", "限/选", 9), ("avail", "空余", 6),
    ],
    "normal": [
        ("no", "课程号", 10), ("name", "课程名", 26), ("cat", "课程类别", 20),
        ("seats", "限/选", 9), ("avail", "空余", 6), ("status", "状态", 9),
    ],
    "compact": [
        ("name", "课程", 22), ("seats", "限/选", 9),
        ("avail", "空余", 6), ("status", "状态", 8),
    ],
    "tiny": [("name", "课程", 18), ("avail", "空余", 6)],
}


class ComposerInput(Input):
    """底部 composer；空白时把传统单键快捷键交还给应用。

    这样用户点进输入框后仍能直接按 ``h``、``f`` 等操作；一旦已经输入
    内容，按键则完全作为普通文本，避免妨碍输入命令。
    """

    _SHORTCUTS = {
        "s": "action_toggle_monitor",
        "r": "action_run_round",
        "n": "action_set_interval_dialog",
        "f": "action_open_filters",
        "c": "action_open_settings",
        "h": "action_open_help",
        "v": "action_toggle_view",
        "q": "action_quit",
    }

    def on_key(self, event) -> None:
        action = self._SHORTCUTS.get(event.key) if not self.value else None
        if action:
            event.prevent_default()
            getattr(self.app, action)()


class ToggleRow(Static):
    """一行"标签: 值"的键盘切换项；←/→（或空格/回车）循环切换。

    替代 Switch / Select 等图形控件，Tab 可在输入框与这些行之间移动
    焦点，聚焦时行首显示 ❯。
    """

    can_focus = True

    def __init__(self, label: str, options: list[tuple[str, str]],
                 value: str, id: Optional[str] = None) -> None:  # noqa: A002
        super().__init__(id=id, classes="toggle")
        self.row_label = label
        self.options = options
        self.value = value

    def set_value(self, value: str) -> None:
        self.value = value
        self._refresh()

    def cycle(self, step: int = 1) -> None:
        values = [v for _l, v in self.options]
        if self.value in values:
            self.set_value(values[(values.index(self.value) + step) % len(values)])
        else:
            self.set_value(values[0])

    def _refresh(self) -> None:
        current = next((l for l, v in self.options if v == self.value), self.value)
        cursor = "❯" if self.has_focus else " "
        self.update(f"{cursor} {self.row_label}: {current}   [dim]←/→ 切换[/]")

    def on_mount(self) -> None:
        self._refresh()

    def on_focus(self) -> None:
        self._refresh()

    def on_blur(self) -> None:
        self._refresh()

    def on_key(self, event) -> None:
        if event.key in ("right", "space", "enter"):
            self.cycle(1)
            event.stop()
            event.prevent_default()
        elif event.key == "left":
            self.cycle(-1)
            event.stop()
            event.prevent_default()


def _risk_text(percent: int, label: str) -> str:
    """按风险等级渲染"风控触发率"，Static 默认启用 rich markup。"""
    color = {"无": "green", "低": "green", "中": "cyan",
             "高": "red", "极高": "red", "已触发/疑似": "bold red"}.get(label, "cyan")
    return f"风控[bold {color}] {percent}%({label})[/]"

APP_CSS = """
/* 终端原生观感：默认前景色为主，dim 次要信息，品牌 magenta。 */
#app_title { height: 1; padding: 0 1; text-style: bold; color: magenta; }
#context { height: 1; padding: 0 1; text-style: dim; }
#status { height: 1; padding: 0 1; text-style: dim; }
#workspace { height: 1fr; padding: 0 1; }
#welcome { height: auto; margin: 2 0 1 0; }
#table { height: 1fr; border: none; }
#log { height: 10; margin-top: 1; border: none; }
#composer { height: 3; margin: 0 1; }
#composer_hint { height: 1; padding: 0 1; text-style: dim; }
DataTable > .datatable--header { text-style: bold; }

/* 弹窗：统一宽度、内边距与边框；屏幕层压暗并居中，避免主界面内容透出。 */
ModalScreen { align: center middle; background: $background 75%; }
#filterscreen { width: 94; height: 82%; padding: 1 2;
                background: $surface; border: round $foreground 40%; }
#helpbox, #settingsbox, #firstrunbox { width: 96; height: 86%; padding: 1 2;
                background: $surface; border: round $foreground 40%; }
#intervalbox { width: 64; height: 9; padding: 1 2; align: center middle;
               background: $surface; border: round $foreground 40%; }
#helpbox { overflow-y: auto; }
#fsbody { height: 1fr; }
#dim_label, #match_label, #fs_hint { text-style: dim; }
#fs_hint { padding: 0 1; }
#cands { width: 3fr; }
#selpanel { width: 2fr; padding: 0 1; }
#sel_list { height: 1fr; overflow: auto; }
#query { margin: 1 0; }

/* 设置页：固定标签列 + 输入列，字段说明不随输入内容消失。 */
#set_hint { text-style: dim; margin-bottom: 1; }
#setscroll { height: 1fr; }
.section { text-style: bold; margin-top: 1; }
.hint { text-style: dim; }
.row { height: 3; }
.field_label { width: 22; height: 3; content-align: left middle; }
.row Input { width: 1fr; }
.toggle { height: 1; margin-top: 1; }
.help-title { text-style: bold; }
"""


# ---------------------------------------------------------------------------
# 帮助
# ---------------------------------------------------------------------------

class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "关闭")]

    def compose(self) -> ComposeResult:
        with Vertical(id="helpbox"):
            yield Label("courser — PKU 补退选空余名额监控", classes="help-title")
            yield Static(
                "命令（底部输入框，和快捷键等价）\n"
                "  /start  /stop  开始 / 停止监控       /fetch 立即抓取一轮\n"
                "  /filters 管理筛选条件                /settings 修改全部设置\n"
                "  /interval [分钟] 修改轮询间隔        /view 切换课程视图\n"
                "  /help 查看本页                        /quit 退出\n"
                "  Ctrl+C 任意界面退出（含弹窗/向导）\n\n"
                "监控流程（每轮重新登录）\n"
                "  登出旧会话 → 打开 IAAA 登录页 → 等待密码管理器自动填充"
                "（或在「设置」里填学号/密码）→ 点登录 → 补退选 → 动态翻页读取 限数/已选\n\n"
                "筛选（/filters 或 f）\n"
                "  顶部输入框即输即滤（pi 风格）；回车切换选中/自定义添加；\n"
                "  支持 课程名 / 课程类别 / 开课院系 三个维度，每维度可多选、可并存；\n"
                "  m 切换「任一命中 / 全部命中」；d 删除条目。\n\n"
                "通知（/settings → 邮件通知）\n"
                "  通过 gws（Google Workspace CLI）发送：请先 `gws auth login` 授权，\n"
                "  并在「设置」填写 收件邮箱（发件账号可选）；同课通知有冷却去重。\n\n"
                "安全与节奏\n"
                "  浏览器以后台窗口运行（不抢焦点，可打开 Dock/任务栏窗口实时查看）；\n"
                "  相邻操作随机间隔、轮询间隔带抖动，模仿人类；\n"
                "  绝不输入验证码，登录失败/风控时自动降速并提示人工处理。\n\n"
                "快捷键\n"
                "  s 开始/停止监控   r 立即抓取一轮   n 改间隔\n"
                "  f 筛选   c 设置   v 视图切换   h 帮助   q 退出\n\n"
                "[dim]按任意键返回[/]",
                id="helptext")

    def action_close(self) -> None:
        self.dismiss(None)

    def on_key(self, event) -> None:
        # 任意键关闭并拦下事件，避免冒泡再次触发应用级绑定
        # （如按 h 关闭后又被应用重新打开）。Ctrl+C 留给应用级退出。
        if event.key != "ctrl+c" and self.app.screen is self:
            event.stop()
            event.prevent_default()
            self.dismiss(None)


# ---------------------------------------------------------------------------
# 筛选管理（pi 风格：顶部查询输入即输即滤）
# ---------------------------------------------------------------------------

class FilterScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "保存并完成"),
        Binding("1", "group(0)", "课程名"),
        Binding("2", "group(1)", "课程类别"),
        Binding("3", "group(2)", "开课院系"),
        Binding("m", "toggle_match", "任一/全部"),
        Binding("c", "add_custom", "自定义添加"),
        Binding("d", "delete_entry", "删除条目"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.group_idx = 0
        self.entries: dict[str, list[str]] = {}
        self.candidates: dict[str, list[str]] = {}
        self._sync_from_cfg()

    def _sync_from_cfg(self) -> None:
        app = self._app()
        cfg = app.cfg
        self.entries = {
            "names": list(cfg.filters.names),
            "categories": list(cfg.filters.categories),
            "depts": list(cfg.filters.depts),
        }
        self.candidates = {
            "names": list(app.candidate_lists["names"]),
            "categories": list(app.candidate_lists["categories"]),
            "depts": list(app.candidate_lists["depts"]),
        }
        for g in GROUPS:
            for e in self.entries[g[0]]:
                if e not in self.candidates[g[0]]:
                    self.candidates[g[0]].append(e)

    def _app(self) -> "CourserApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical(id="filterscreen"):
            yield Label("筛选管理 — 顶部输入即输即滤（pi 风格）", classes="help-title")
            yield Static(id="dim_label")
            yield Static(id="match_label")
            yield Input(placeholder="查询：输入即过滤；回车添加/切换选中", id="query")
            with Horizontal(id="fsbody"):
                yield ListView(id="cands")
                with Vertical(id="selpanel"):
                    yield Label("已选条目（↑↓ 选中后按 d 删除）")
                    yield Static(id="sel_list", classes="panel")
            yield Static(id="fs_hint", classes="hint", markup=True)

    def on_mount(self) -> None:
        self._apply_match_label()
        self._render_dim_label()
        self._render_list()
        self._render_entries()
        self.query_one("#query", Input).focus()
        self.query_one("#fs_hint", Static).update(
            "[dim]1/2/3 维度  ↑↓ 选择  Enter 切换选中  c 自定义  d 删除"
            "  m 任一/全部  Esc 完成并保存[/]")

    def _group_id(self) -> str:
        return GROUPS[self.group_idx][0]

    def _query(self) -> str:
        return self.query_one("#query", Input).value.strip()

    def _filtered(self) -> list[str]:
        q = self._query()
        cands = self.candidates[self._group_id()]
        if not q:
            return list(cands)
        return [c for c in cands if q.lower() in c.lower()]

    def _render_dim_label(self) -> None:
        self.query_one("#dim_label", Static).update(
            "维度：[bold]1[/] 课程名 · [bold]2[/] 课程类别 · [bold]3[/] 开课院系"
            f"（当前 [bold]{GROUPS[self.group_idx][1]}[/]）")

    def _render_list(self) -> None:
        lv = self.query_one("#cands", ListView)
        items = self._filtered()
        entries = set(self.entries[self._group_id()])
        lv.clear()
        for it in items:
            mark = "✓ " if it in entries else "  "
            lv.append(ListItem(Label(mark + it)))
        if items and (lv.index is None or lv.index >= len(items)):
            lv.index = len(items) - 1 if lv.index is not None else 0

    def _render_entries(self) -> None:
        lines = []
        for gid, gname in GROUPS:
            es = self.entries[gid]
            lines.append(f"[bold]{gname}[/] ({len(es)})")
            for e in es:
                lines.append(f"  {e}")
        self.query_one("#sel_list", Static).update("\n".join(lines) or "[dim]（空）[/]")

    def _toggle(self, value: str) -> None:
        gid = self._group_id()
        if value in self.entries[gid]:
            self.entries[gid].remove(value)
        else:
            self.entries[gid].append(value)
        self._render_list()
        self._render_entries()

    # -- 事件 -------------------------------------------------------------
    @on(Input.Changed, "#query")
    def _on_query_changed(self, event: Input.Changed) -> None:
        self._render_list()

    @on(Input.Submitted, "#query")
    def _on_query_submit(self, event: Input.Submitted) -> None:
        q = self._query()
        if not q:
            return
        if (q in self.candidates[self._group_id()] or q in self.entries[self._group_id()]):
            self._toggle(q)
            self.query_one("#query", Input).value = ""
        else:
            self.action_add_custom()

    @on(ListView.Selected, "#cands")
    def _on_list_selected(self, event: ListView.Selected) -> None:
        lv = self.query_one("#cands", ListView)
        items = self._filtered()
        if lv.index is not None and 0 <= lv.index < len(items):
            self._toggle(items[lv.index])

    def on_key(self, event) -> None:
        q = self.query_one("#query", Input)
        if q.has_focus and event.key in ("down", "up"):
            lv = self.query_one("#cands", ListView)
            items = self._filtered()
            if not items:
                event.stop()
                return
            if lv.index is None:
                lv.index = 0
            elif event.key == "down":
                lv.action_cursor_down()
            else:
                lv.action_cursor_up()
            event.stop()

    # -- actions ----------------------------------------------------------
    def action_group(self, i: str) -> None:
        self.group_idx = int(i) % len(GROUPS)
        self._render_dim_label()
        self._render_list()

    def action_toggle_match(self) -> None:
        cfg = self._app().cfg
        cfg.filters.match = "all" if cfg.filters.match != "all" else "any"
        self._apply_match_label()

    def _apply_match_label(self) -> None:
        mode = self._app().cfg.filters.match
        text = ("命中模式：[bold green]任一命中[/]（命中任意一个维度即告警）"
                if mode == "any" else
                "命中模式：[bold]全部命中[/]（所有非空维度都命中才告警）")
        self.query_one("#match_label", Static).update(text)

    def action_add_custom(self) -> None:
        q = self._query()
        if not q:
            return
        gid = self._group_id()
        if q not in self.entries[gid]:
            self.entries[gid].append(q)
            if q not in self.candidates[gid]:
                self.candidates[gid].append(q)
            self._render_list()
            self._render_entries()

    def action_delete_entry(self) -> None:
        lv = self.query_one("#cands", ListView)
        items = self._filtered()
        if lv.index is not None and 0 <= lv.index < len(items):
            value = items[lv.index]
            gid = self._group_id()
            if value in self.entries[gid]:
                self.entries[gid].remove(value)
                self._render_list()
                self._render_entries()

    def action_close(self) -> None:
        cfg = self._app().cfg
        cfg.filters.names = self.entries["names"]
        cfg.filters.categories = self.entries["categories"]
        cfg.filters.depts = self.entries["depts"]
        cfg.save()
        self._app().log_line("筛选条件已保存")
        self.dismiss(None)


# ---------------------------------------------------------------------------
# 首次配置向导（初次启动强制出现；之后仍可在「设置」中修改）
# ---------------------------------------------------------------------------

class FirstRunScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "later", "稍后再说"),
        Binding("1", "go_settings", "前往设置"),
        Binding("2", "done", "已完成配置"),
    ]

    def _app(self) -> "CourserApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical(id="firstrunbox"):
            yield Label("首次使用 courser — 请先完成两项配置", classes="help-title")
            yield Static(
                "开始监控前需要先配置好以下内容（以后仍可在「设置」中修改）：\n\n"
                "1. gws（Google Workspace CLI）——负责发送提醒邮件\n"
                "   安装并授权：\n"
                "     brew install gws     （或 npm i -g @googleworkspace/cli）\n"
                "     gws auth login       （浏览器完成 OAuth2 授权，仅一次）\n\n"
                "2. 在「设置」中填写：\n"
                "   · 收件邮箱 —— 提醒邮件发送到的地址\n"
                "   · gws 发件账号（你的 Gmail，可选）\n"
                "   建议顺手用 ctrl+t 发送测试邮件验证。\n\n"
                "（学号/密码、筛选条件、轮询间隔也都在「设置」中；\n"
                "   学号/密码留空则依赖浏览器密码管理器自动填充。）\n\n"
                "[dim]1 前往设置   2 我已配置完成   Esc 稍后再说[/]",
                id="firstruntext")

    def action_go_settings(self) -> None:
        self._app().action_open_settings()

    def action_done(self) -> None:
        self._app().cfg.first_run_done = True
        self._app().cfg.save()
        self._app().log_line("首次配置完成，可以开始监控了")
        self.dismiss(None)

    def action_later(self) -> None:
        self._app().log_line("提示：完成配置前无法启动监控（gws + 收件邮箱）")
        self.dismiss(None)


# ---------------------------------------------------------------------------
# 设置（唯一入口：所有配置集中在此）
# ---------------------------------------------------------------------------

class SettingsScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "cancel", "取消"),
        Binding("ctrl+s", "save", "保存"),
        Binding("ctrl+t", "test_mail", "测试邮件"),
        Binding("w", "cycle_window", "窗口模式"),
        Binding("o", "cycle_match", "筛选组合"),
        Binding("r", "toggle_relogin", "强制重登"),
    ]

    def _app(self) -> "CourserApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical(id="settingsbox"):
            yield Label("设置 — 所有配置集中在此处", classes="help-title")
            yield Static("tab 切换字段 · w 窗口 · o 组合 · r 强制重登 · "
                         "ctrl+t 测试邮件 · ctrl+s 保存 · esc 取消", id="set_hint")
            with VerticalScroll(id="setscroll"):
                yield Label("账号凭据（可选；留空则依赖浏览器密码管理器自动填充）",
                            classes="section")
                with Horizontal(classes="row"):
                    yield Label("学号", classes="field_label")
                    yield Input(id="set_user")
                with Horizontal(classes="row"):
                    yield Label("密码", classes="field_label")
                    yield Input(id="set_pass", password=True)

                yield Label("邮件通知（通过 gws 发送，需先 `gws auth login` 授权）",
                            classes="section")
                yield Static(id="set_gws_status", classes="hint")
                with Horizontal(classes="row"):
                    yield Label("收件邮箱", classes="field_label")
                    yield Input(id="set_to")
                with Horizontal(classes="row"):
                    yield Label("gws 发件账号", classes="field_label")
                    yield Input(id="set_gws_from")
                with Horizontal(classes="row"):
                    yield Label("每小时最多发送", classes="field_label")
                    yield Input(id="set_mail_limit")
                with Horizontal(classes="row"):
                    yield Label("同课通知冷却（分钟）", classes="field_label")
                    yield Input(id="set_mail_cooldown")

                yield Label("轮询节奏（自动带随机抖动）", classes="section")
                with Horizontal(classes="row"):
                    yield Label("轮询间隔（分钟）", classes="field_label")
                    yield Input(id="set_interval")
                with Horizontal(classes="row"):
                    yield Label("间隔抖动（0~1）", classes="field_label")
                    yield Input(id="set_jitter")
                with Horizontal(classes="row"):
                    yield Label("翻页间隔下限（秒）", classes="field_label")
                    yield Input(id="set_pd_min")
                with Horizontal(classes="row"):
                    yield Label("翻页间隔上限（秒）", classes="field_label")
                    yield Input(id="set_pd_max")

                yield Label("行为", classes="section")
                with Horizontal(classes="row"):
                    yield Label("opencli 会话名", classes="field_label")
                    yield Input(id="set_session")
                yield ToggleRow("浏览器窗口", [
                    ("后台窗口（不抢焦点）", "background"),
                    ("前台窗口", "foreground")], "background", id="tgl_window")
                yield ToggleRow("筛选组合", [
                    ("任一命中", "any"), ("全部命中", "all")], "any", id="tgl_match")
                yield ToggleRow("每轮强制重新登录", [
                    ("关", "off"), ("开", "on")], "off", id="tgl_relogin")

    def on_mount(self) -> None:
        cfg = self._app().cfg
        c, n = cfg.credentials, cfg.notify
        self.query_one("#set_user", Input).value = c.username
        self.query_one("#set_pass", Input).value = c.password
        gws_ok = "已安装" if notifier.gws_available() else "未找到 gws 命令"
        self.query_one("#set_gws_status", Static).update(
            f"gws 状态：{gws_ok}（安装后执行 gws auth login 授权）")
        self.query_one("#set_to", Input).value = n.to
        self.query_one("#set_gws_from", Input).value = n.gws_from
        self.query_one("#set_mail_limit", Input).value = str(n.max_per_hour)
        self.query_one("#set_mail_cooldown", Input).value = str(n.min_interval_min)
        self.query_one("#set_interval", Input).value = str(cfg.interval_min)
        self.query_one("#set_jitter", Input).value = str(cfg.interval_jitter)
        self.query_one("#set_pd_min", Input).value = str(cfg.page_delay_min)
        self.query_one("#set_pd_max", Input).value = str(cfg.page_delay_max)
        self.query_one("#set_session", Input).value = cfg.session
        self.query_one("#tgl_window", ToggleRow).set_value(cfg.window)
        self.query_one("#tgl_match", ToggleRow).set_value(cfg.filters.match)
        self.query_one("#tgl_relogin", ToggleRow).set_value(
            "on" if cfg.force_relogin else "off")

    def _float(self, iid: str, default: float) -> float:
        try:
            return float(self.query_one(iid, Input).value.strip())
        except ValueError:
            return default

    def _apply(self) -> None:
        cfg = self._app().cfg
        cfg.credentials.username = self.query_one("#set_user", Input).value.strip()
        cfg.credentials.password = self.query_one("#set_pass", Input).value
        n = cfg.notify
        n.to = self.query_one("#set_to", Input).value.strip()
        n.gws_from = self.query_one("#set_gws_from", Input).value.strip()
        n.max_per_hour = max(1, int(self._float("#set_mail_limit", 5)))
        n.min_interval_min = self._float("#set_mail_cooldown", 15.0)
        cfg.interval_min = self._float("#set_interval", 8.0)
        cfg.interval_jitter = self._float("#set_jitter", 0.4)
        cfg.page_delay_min = self._float("#set_pd_min", 6.0)
        cfg.page_delay_max = self._float("#set_pd_max", 14.0)
        cfg.session = self.query_one("#set_session", Input).value.strip() or "courser-watch"
        cfg.window = self.query_one("#tgl_window", ToggleRow).value
        cfg.filters.match = self.query_one("#tgl_match", ToggleRow).value
        cfg.force_relogin = self.query_one("#tgl_relogin", ToggleRow).value == "on"
        cfg.first_run_done = True
        cfg.save()

    def action_save(self) -> None:
        self._apply()
        self._app().log_line("设置已保存，首次配置完成")
        self.dismiss(None)
        self._app()._maybe_close_first_run()

    def action_cancel(self) -> None:
        self.dismiss(None)

    def action_test_mail(self) -> None:
        self._apply()
        cfg = self._app().cfg
        self._app().log_line("正在发送测试邮件…")
        ok = notifier.send_email(cfg.notify, "【courser】测试邮件",
                                 "这是 courser 发送的测试邮件。收到说明邮件通知配置正常。",
                                 log=self._app().log_line)
        if ok:
            self._app().notify("测试邮件已发送", timeout=5)

    def _toggle_row(self, iid: str) -> ToggleRow:
        return self.query_one(iid, ToggleRow)

    def action_cycle_window(self) -> None:
        self._toggle_row("#tgl_window").cycle()

    def action_cycle_match(self) -> None:
        self._toggle_row("#tgl_match").cycle()

    def action_toggle_relogin(self) -> None:
        self._toggle_row("#tgl_relogin").cycle()


# ---------------------------------------------------------------------------
# 修改轮询间隔的小弹窗
# ---------------------------------------------------------------------------

class IntervalModal(ModalScreen[None]):
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def __init__(self, current: float, on_submit) -> None:
        super().__init__()
        self.current = current
        self.on_submit = on_submit

    def compose(self) -> ComposeResult:
        with Vertical(id="intervalbox"):
            yield Label(f"修改轮询间隔（分钟，当前 {self.current}）")
            yield Input(placeholder="新间隔（分钟）", id="interval_input")
            yield Static("[dim]回车确认 · esc 取消[/]", classes="hint")

    def on_mount(self) -> None:
        self.query_one("#interval_input", Input).focus()

    @on(Input.Submitted, "#interval_input")
    def _submitted(self, event: Input.Submitted) -> None:
        self.on_submit(event.value)
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------

class CourserApp(App):
    TITLE = "courser"
    SUB_TITLE = "PKU 补退选空余名额监控（opencli + textual）"
    CSS = APP_CSS
    BINDINGS = [
        # 应用级高优先级绑定：即使焦点在 Input 或任一 ModalScreen 中也可退出。
        Binding("ctrl+c", "quit", "退出", show=False, priority=True),
        Binding("s", "toggle_monitor", "开始/停止"),
        Binding("r", "run_round", "立即抓取"),
        Binding("n", "set_interval_dialog", "间隔"),
        Binding("f", "open_filters", "筛选"),
        Binding("c", "open_settings", "设置"),
        Binding("h", "open_help", "帮助"),
        Binding("v", "toggle_view", "视图"),
        Binding("q", "quit", "退出"),
    ]

    VIEWS = ["all", "matched", "seats"]
    VIEW_NAMES = {"all": "全部课程", "matched": "命中·按类别", "seats": "有空余名额"}

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.courses: list[Course] = []
        self.snapshot_ts: Optional[str] = None
        self.candidate_lists = {"names": [], "categories": [], "depts": []}
        self.view = "all"
        self.watcher: Optional[Watcher] = None
        self._table_layout = ""
        self._load_snapshot()

    # -- 数据持久化 -------------------------------------------------------
    def _load_snapshot(self) -> None:
        for p in (Path("data/last_round.json"), Path("data/courses_snapshot.json")):
            if p.exists():
                try:
                    d = json.loads(p.read_text(encoding="utf-8"))
                    self.courses = [Course(**{k: v for k, v in c.items()})
                                    for c in d.get("courses", [])]
                    self.snapshot_ts = d.get("ts")
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
        d = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "pages": r.pages,
             "n": len(r.courses), "distinct": distinct,
             "courses": [c.__dict__ for c in r.courses]}
        Path("data").mkdir(exist_ok=True)
        Path("data/last_round.json").write_text(
            json.dumps(d, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # -- 日志（线程安全） -------------------------------------------------
    def log_line(self, msg: str) -> None:
        try:
            self.query_one("#log", RichLog).write(f"{time.strftime('%H:%M:%S')}  {msg}")
        except Exception:
            pass

    def _thread_log(self, msg: str) -> None:
        self.call_from_thread(self.log_line, msg)

    # -- UI --------------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static("courser", id="app_title")
        yield Static("PKU course vacancy monitor · /help for commands", id="context")
        yield Static(id="status")
        with Vertical(id="workspace"):
            yield Static(id="welcome", markup=True)
            yield DataTable(id="table")
            yield RichLog(id="log", highlight=True, markup=False, wrap=True)
        yield ComposerInput(placeholder="输入命令，例如 /start；输入 /help 查看全部命令",
                            id="composer")
        yield Static("s 开始/停止 · r 抓取 · f 筛选 · c 设置 · n 间隔 · v 视图 · q 退出",
                     id="composer_hint")

    def on_mount(self) -> None:
        self.watcher = Watcher(self.cfg, log=self._thread_log, on_round=self._on_round)
        self._setup_table()
        self.render_table()
        self._apply_responsive_layout(self.size.width, self.size.height)
        self.set_interval(1.0, self._tick)
        self.log_line(f"启动：筛选 {FilterSet(self.cfg.filters).describe()}")
        self.log_line("就绪：输入 /help 查看命令；快捷键仍然可用。")
        self.call_after_refresh(self._maybe_first_run)

    def _maybe_first_run(self) -> None:
        if not self.cfg.first_run_done and not isinstance(self.screen, FirstRunScreen):
            self.push_screen(FirstRunScreen())
            self.log_line("首次使用：请先完成配置（gws + 收件邮箱）")

    def _maybe_close_first_run(self) -> None:
        if isinstance(self.screen, FirstRunScreen) and self.cfg.first_run_done:
            self.pop_screen()

    def _setup_table(self) -> None:
        self._configure_table(force=True)

    def _table_layout_for_width(self, width: int) -> str:
        if width < 48:
            return "tiny"
        if width < 72:
            return "compact"
        if width < 118:
            return "normal"
        return "wide"

    def _configure_table(self, force: bool = False, width: Optional[int] = None) -> bool:
        """按当前终端宽度重建列，避免 DataTable 横向挤压。"""
        layout = self._table_layout_for_width(width if width is not None else self.size.width)
        if not force and layout == self._table_layout:
            return False
        dt = self.query_one("#table", DataTable)
        dt.clear(columns=True)
        for key, label, width in TABLE_LAYOUTS[layout]:
            dt.add_column(label, key=key, width=width)
        self._table_layout = layout
        return True

    def render_table(self) -> None:
        dt = self.query_one("#table", DataTable)
        dt.clear()
        welcome = self.query_one("#welcome", Static)
        if not self.courses:
            welcome.update(
                "[bold]欢迎使用 courser[/]\n\n"
                "监控北京大学补退选课程的空余名额，并在命中筛选条件时通知你。\n\n"
                "[cyan]/start[/] 开始监控    [cyan]/fetch[/] 立即抓取一轮\n"
                "[cyan]/filters[/] 设置课程筛选    [cyan]/settings[/] 配置账号、邮件与节奏\n\n"
                "结果会显示在这里；运行过程会像对话记录一样保留在下方。")
            welcome.display = True
            dt.display = False
            return
        welcome.display = False
        dt.display = True
        fs = FilterSet(self.cfg.filters)
        columns = [key for key, _label, _width in TABLE_LAYOUTS[self._table_layout]]
        # 命中视图：最近一次成功抓取中经过筛选的课程（不限空余），按课程类别排序
        rows = list(self.courses)
        if self.view == "matched":
            rows = [c for c in rows if fs.matches(c)]
            rows.sort(key=lambda c: c.category)  # 稳定排序：同类别保持选课网顺序
        elif self.view == "seats":
            rows = [c for c in rows if c.has_seats]
        for c in rows:
            matched = fs.matches(c)
            seats = Text(f"{c.selected}/{c.quota}" if c.quota is not None else c.seats_raw)
            avail = Text(str(c.avail), style="bold green" if c.has_seats else "dim red")
            name = Text(("★ " if matched else "") + c.name,
                        style="bold" if matched else "default")
            values = {
                "no": c.course_no, "name": name, "cat": c.category, "dept": c.dept,
                "teacher": c.teacher, "seats": seats, "avail": avail, "status": c.status,
                "page": str(c.page) if c.page else "—",
            }
            dt.add_row(*(values[column] for column in columns), key=c.key)

    def on_resize(self, event: events.Resize) -> None:
        """随终端尺寸在四档表格、状态文字与垂直空间间切换。"""
        if not self.is_mounted:
            return
        self._apply_responsive_layout(event.size.width, event.size.height)

    def _apply_responsive_layout(self, width: int, height: int) -> None:
        """应用与尺寸无关的重排逻辑，供挂载和 resize 事件共用。"""
        if self._configure_table(width=width):
            self.render_table()

        self.query_one("#log", RichLog).styles.height = max(4, min(10, height // 3))
        self.query_one("#composer_hint", Static).display = height >= 16
        self.query_one("#context", Static).display = width >= 56 and height >= 14
        self.query_one("#app_title", Static).update(
            "courser" if width >= 40 else "courser · /help")
        hint = self.query_one("#composer_hint", Static)
        hint.update(
            "s 开始/停止 · r 抓取 · f 筛选 · c 设置 · n 间隔 · v 视图 · q 退出"
            if width >= 72 else "s 监控 · r 抓取 · f 筛选 · c 设置 · /help")

        welcome = self.query_one("#welcome", Static)
        if not self.courses:
            welcome.update(
                "[bold]欢迎使用 courser[/]\n\n[cyan]/start[/] 开始监控  [cyan]/fetch[/] 立即抓取\n"
                "[cyan]/filters[/] 筛选  [cyan]/settings[/] 设置"
                if height < 18 or width < 56 else
                "[bold]欢迎使用 courser[/]\n\n"
                "监控北京大学补退选课程的空余名额，并在命中筛选条件时通知你。\n\n"
                "[cyan]/start[/] 开始监控    [cyan]/fetch[/] 立即抓取一轮\n"
                "[cyan]/filters[/] 设置课程筛选    [cyan]/settings[/] 配置账号、邮件与节奏\n\n"
                "结果会显示在这里；运行过程会像对话记录一样保留在下方。"
            )

    # -- composer / events ------------------------------------------------
    @on(Input.Submitted, "#composer")
    def _on_command(self, event: Input.Submitted) -> None:
        raw = event.value.strip()
        composer = self.query_one("#composer", Input)
        composer.value = ""
        if not raw:
            return
        command, _, arg = raw.lower().partition(" ")
        aliases = {
            "/start": self.action_toggle_monitor,
            "/stop": self.action_toggle_monitor,
            "/fetch": self.action_run_round,
            "/filters": self.action_open_filters,
            "/settings": self.action_open_settings,
            "/view": self.action_toggle_view,
            "/help": self.action_open_help,
            "/quit": self.action_quit,
        }
        if command in {"/interval", "/every"}:
            if arg:
                self._set_interval(arg)
            else:
                self.action_set_interval_dialog()
            return
        action = aliases.get(command)
        if action:
            if command == "/start" and self.watcher and self.watcher.running:
                self.log_line("监控已经在运行；使用 /stop 停止。")
            elif command == "/stop" and self.watcher and not self.watcher.running:
                self.log_line("监控尚未启动；使用 /start 开始。")
            else:
                action()
            return
        self.log_line(f"未识别命令：{raw}。输入 /help 查看可用命令。")

    @on(DataTable.RowSelected, "#table")
    def _row_selected(self, event: DataTable.RowSelected) -> None:
        key = event.row_key.value
        for c in self.courses:
            if c.key == key:
                self.log_line(f"选中：{c.name} [{c.course_no}] {c.category} {c.dept} "
                              f"限/选 {c.seats_raw} 状态 {c.status}")
                break

    # -- 轮询完成后回调（watcher 线程 → UI） ------------------------------
    def _on_round(self, r: RoundResult) -> None:
        self.call_from_thread(self._apply_round, r)

    def _apply_round(self, r: RoundResult) -> None:
        self.snapshot_ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if r.ok and r.courses:
            self.courses = r.courses
            self._save_snapshot(r)
        self.render_table()
        if r.notified:
            self.notify(f"已发送提醒邮件：{'、'.join(c.name for c in r.notified)}",
                        severity="information", timeout=8)

    # -- 定时刷新状态栏 ---------------------------------------------------
    def _tick(self) -> None:
        w = self.watcher
        if w is None:
            return
        st = self.query_one("#status", Static)
        run_state = "监控中" if w.running else "未开始"
        countdown = f"{w.countdown_s}s" if w.countdown_s is not None else "--"
        last = ""
        if w.last_result:
            last = (f"{w.last_result.pages}页/{w.last_result.total}课 "
                    f"{w.last_result.duration_s:.0f}s"
                    + (" 成功" if w.last_result.ok else " 失败"))
        risk = "风控 --" if not w.last_result else _risk_text(w.last_result.risk_percent,
                                                             w.last_result.risk_label)
        fs = FilterSet(self.cfg.filters)
        n = self.cfg.notify
        gws_txt = "gws ✓" if notifier.gws_available() else "gws ✗"
        st.update(
            f"{run_state} · 下一轮 {countdown} · 上一轮 {last or '—'} · {risk}"
            f" · {self.VIEW_NAMES[self.view]} · {fs.describe()} · 邮件 {gws_txt}"
            + (" 已配置" if n.configured and notifier.gws_available() else " 未配置"))

    # -- actions ----------------------------------------------------------
    def action_toggle_monitor(self) -> None:
        w = self.watcher
        if w is None:
            return
        if not self.cfg.first_run_done:
            self.notify("首次使用请先完成配置（gws + 收件邮箱），已为你打开设置",
                        severity="warning", timeout=6)
            self.action_open_settings()
            return
        if w.running:
            w.stop()
            self.log_line("监控已停止")
        else:
            w.start()
            self.log_line(f"监控开始：每约 {self.cfg.interval_min} 分钟一轮（带抖动）")

    @work(thread=True, exclusive=True)
    def action_run_round(self) -> None:
        w = self.watcher
        if w is None:
            return
        self.log_line("手动触发一轮抓取…")
        w.run_round()

    def action_open_filters(self) -> None:
        self.push_screen(FilterScreen())

    def action_open_settings(self) -> None:
        self.push_screen(SettingsScreen())

    def action_open_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_toggle_view(self) -> None:
        self.view = self.VIEWS[(self.VIEWS.index(self.view) + 1) % len(self.VIEWS)]
        self.render_table()

    def action_set_interval_dialog(self) -> None:
        self.push_screen(IntervalModal(self.cfg.interval_min, self._set_interval))

    def _set_interval(self, value: str) -> None:
        try:
            self.cfg.interval_min = max(1.0, float(value))
            self.cfg.save()
            self.log_line(f"轮询间隔已设为 {self.cfg.interval_min} 分钟（带抖动）")
        except ValueError:
            self.log_line("间隔格式错误，未修改")

    def on_unmount(self) -> None:
        if self.watcher:
            self.watcher.stop()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="courser", description="PKU 补退选空余名额监控 TUI")
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

    # mouse=False：纯键盘操作，终端不上报鼠标事件。
    CourserApp(cfg).run(mouse=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
