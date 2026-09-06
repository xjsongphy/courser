"""courser 的 Textual TUI。

布局：菜单栏（监控 / 筛选 / 设置 / 帮助）+ 状态栏 + 课程表格 + 日志。
- 菜单是主要操作入口；快捷键仅为辅助（底部 Footer 有提示）。
- 所有配置统一放在「设置」一个入口里。
- 「筛选」提供 pi 风格的筛选条件管理：顶部查询输入框即输即滤，
  回车添加/切换选中，支持 课程名 / 课程类别 / 开课院系 三个维度多选并存。
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Optional

from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import (Button, DataTable, Footer, Header, Input, Label,
                             ListItem, ListView, RichLog, Select, Static, Switch)

from . import notifier
from .config import Config, load_env_file
from .filters import FilterSet
from .fetch import Course
from .watcher import RoundResult, Watcher

GROUPS = [("names", "课程名"), ("categories", "课程类别"), ("depts", "开课院系")]


def _risk_text(percent: int, label: str) -> str:
    """按风险等级渲染"风控触发率"，Static 默认启用 rich markup。"""
    color = {"无": "green", "低": "green", "中": "yellow",
             "高": "red", "极高": "red", "已触发/疑似": "bold red"}.get(label, "yellow")
    return f"风控[bold {color}] {percent}%({label})[/]"

APP_CSS = """
CourserApp { background: #101014; }
#menubar { height: 3; padding: 0 1; align: left middle; }
#menubar Button { margin: 0 1; }
#status { height: 3; padding: 0 2; background: #1a1a22; color: #c8c8d4;
          content-align: left middle; }
#body { height: 1fr; }
#left { width: 3fr; }
#right { width: 1fr; padding: 0 1; }
#table { height: 1fr; border: round #33334a; }
#log { height: 9; border: round #33334a; background: #0c0c12; }
#filters_panel { height: 8; border: round #33334a; padding: 0 1; overflow: auto; }
#mail_panel { height: 5; border: round #33334a; padding: 0 1; overflow: auto; }
DataTable { background: #12121a; }
DataTable > .datatable--header { background: #23233a; color: #9adcff; }

/* 弹窗 */
#filterscreen { width: 94; height: 82%; margin: 1 2; background: #16161e;
                border: round #44446a; padding: 1 2; }
#helpbox, #settingsbox { width: 96; height: 86%; background: #16161e;
                         border: round #44446a; padding: 1 2; }
#intervalbox { width: 60; height: 9; background: #16161e; border: round #44446a;
               padding: 1 2; align: center middle; }
#firstrunbox { width: 88; height: 62%; background: #16161e; border: round #44446a;
               padding: 1 2; }
#frbtns { height: 4; align: center middle; }
#frbtns Button { margin: 0 1; }
#grouprow { height: 3; align: left middle; }
#grouprow Button { margin: 0 1; }
#query { margin: 1 0; }
#fsbody { height: 1fr; }
#cands { width: 3fr; border: round #33334a; }
#selpanel { width: 2fr; padding: 0 1; }
#sel_list { height: 1fr; border: round #33334a; overflow: auto; }
#fs_hint { height: 3; padding: 0 1; color: #88889a; }
#setbtns { height: 4; align: center middle; }
#setbtns Button { margin: 0 1; }
Label { margin-top: 1; }
Input { margin-bottom: 1; }
.help-title { text-style: bold; color: #9adcff; }
"""


# ---------------------------------------------------------------------------
# 帮助
# ---------------------------------------------------------------------------

class HelpScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "close", "关闭")]

    def compose(self) -> ComposeResult:
        with Vertical(id="helpbox"):
            yield Label("[bold cyan]courser[/] — PKU 补退选空余名额监控", classes="help-title")
            yield Static(
                "■ 监控流程（每轮重新登录）\n"
                "  登出旧会话 → 打开 IAAA 登录页 → 等待密码管理器自动填充"
                "（或在「设置」里填学号/密码）→ 点登录 → 补退选 → 动态翻页读取 限数/已选\n\n"
                "■ 筛选（菜单「筛选」）\n"
                "  顶部输入框即输即滤（pi 风格）；回车切换选中/自定义添加；\n"
                "  支持 课程名 / 课程类别 / 开课院系 三个维度，每维度可多选、可并存；\n"
                "  m 切换「任一命中 / 全部命中」；d 删除条目。\n\n"
                "■ 通知（菜单「设置」→ 邮件通知）\n"
                "  通过 gws（Google Workspace CLI）发送：请先 `gws auth login` 授权，\n"
                "  并在「设置」填写 收件邮箱（发件账号可选）；同课通知有冷却去重。\n\n"
                "■ 安全与节奏\n"
                "  浏览器以后台窗口运行（不抢焦点，可点开 Dock/任务栏窗口实时查看）；\n"
                "  相邻操作随机间隔、轮询间隔带抖动，模仿人类；\n"
                "  绝不输入验证码，登录失败/风控时自动降速并提示人工处理。\n\n"
                "■ 快捷键（辅助，菜单为主）\n"
                "  s 开始/停止监控   r 立即抓取一轮   n 改间隔\n"
                "  f 筛选   c 设置   v 视图切换   h 帮助   q 退出",
                id="helptext")
            yield Button("关闭", id="help_close", variant="primary")

    @on(Button.Pressed, "#help_close")
    def _close(self, event: Button.Pressed) -> None:
        self.dismiss(None)


# ---------------------------------------------------------------------------
# 筛选管理（pi 风格：顶部查询输入即输即滤）
# ---------------------------------------------------------------------------

class FilterScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close", "完成"),
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
            yield Label("[bold cyan]筛选管理[/] — 顶部输入即输即滤（pi 风格）",
                        classes="help-title")
            with Horizontal(id="grouprow"):
                for _gid, gname in GROUPS:
                    yield Button(gname, id=f"grp_{_gid}", classes="groupbtn")
            yield Static(id="match_label")
            yield Input(placeholder="🔍 查询：输入即过滤；回车添加/切换选中（如：通识课(通选课III) / 英语）",
                        id="query")
            with Horizontal(id="fsbody"):
                yield ListView(id="cands")
                with Vertical(id="selpanel"):
                    yield Label("已选条目（↑↓ 选中后按 d 删除）")
                    yield Static(id="sel_list", classes="panel")
            yield Static(id="fs_hint", classes="hint", markup=True)
        yield Button("✔ 完成", id="fs_done", variant="success")

    def on_mount(self) -> None:
        self._apply_match_label()
        self._render_group_buttons()
        self._render_list()
        self._render_entries()
        self.query_one("#query", Input).focus()
        self.query_one("#fs_hint", Static).update(
            "[dim]1/2/3 切换维度  ↑↓ 选择  Enter 切换选中  c 自定义添加  d 删除"
            "  m 任一/全部  Esc 完成[/]")

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

    def _render_group_buttons(self) -> None:
        for i, (gid, _gname) in enumerate(GROUPS):
            b = self.query_one(f"#grp_{gid}", Button)
            b.variant = "primary" if i == self.group_idx else "default"

    def _apply_match_label(self) -> None:
        mode = self._app().cfg.filters.match
        text = ("命中模式：[bold green]任一命中[/]（命中任意一个维度即告警）"
                if mode == "any" else
                "命中模式：[bold yellow]全部命中[/]（所有非空维度都命中才告警）")
        self.query_one("#match_label", Static).update(text)

    def _render_list(self) -> None:
        lv = self.query_one("#cands", ListView)
        items = self._filtered()
        entries = set(self.entries[self._group_id()])
        lv.clear()
        for it in items:
            mark = "✔ " if it in entries else "  "
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

    @on(Button.Pressed, "#fs_done")
    def _done(self, event: Button.Pressed) -> None:
        self.action_close()

    @on(Button.Pressed)
    def _on_group_button(self, event: Button.Pressed) -> None:
        if event.button.id and event.button.id.startswith("grp_"):
            gid = event.button.id[4:]
            self.group_idx = next(i for i, (g, _n) in enumerate(GROUPS) if g == gid)
            self._render_group_buttons()
            self._render_list()

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
        self._render_group_buttons()
        self._render_list()

    def action_toggle_match(self) -> None:
        cfg = self._app().cfg
        cfg.filters.match = "all" if cfg.filters.match != "all" else "any"
        self._apply_match_label()

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
        self._app().log_line("✔ 筛选条件已保存")
        self.dismiss(None)


# ---------------------------------------------------------------------------
# 首次配置向导（初次启动强制出现；之后仍可在「设置」中修改）
# ---------------------------------------------------------------------------

class FirstRunScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "later", "稍后再说")]

    def _app(self) -> "CourserApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical(id="firstrunbox"):
            yield Label("[bold cyan]首次使用 courser[/] — 请先完成两项配置",
                        classes="help-title")
            yield Static(
                "开始监控前需要先配置好以下内容（以后仍可在「⚙ 设置」修改）：\n\n"
                "1. gws（Google Workspace CLI）——负责发送提醒邮件\n"
                "   安装并授权：\n"
                "     brew install gws     （或 npm i -g @googleworkspace/cli）\n"
                "     gws auth login       （浏览器完成 OAuth2 授权，仅一次）\n\n"
                "2. 在「⚙ 设置」中填写：\n"
                "   · 收件邮箱 —— 提醒邮件发送到的地址\n"
                "   · gws 发件账号（你的 Gmail，可选）\n"
                "   建议顺手点「📧 发送测试邮件」验证。\n\n"
                "（学号/密码、筛选条件、轮询间隔也都在「设置」中；\n"
                "   学号/密码留空则依赖浏览器密码管理器自动填充。）",
                id="firstruntext")
            with Horizontal(id="frbtns"):
                yield Button("⚙ 前往设置", id="fr_settings", variant="primary")
                yield Button("✓ 我已配置完成", id="fr_done", variant="success")
                yield Button("稍后再说", id="fr_later", variant="default")

    @on(Button.Pressed, "#fr_settings")
    def _go_settings(self, event: Button.Pressed) -> None:
        self._app().action_open_settings()

    @on(Button.Pressed, "#fr_done")
    def _done(self, event: Button.Pressed) -> None:
        self._app().cfg.first_run_done = True
        self._app().cfg.save()
        self._app().log_line("✔ 首次配置完成，可以开始监控了")
        self.dismiss(None)

    @on(Button.Pressed, "#fr_later")
    def _later(self, event: Button.Pressed) -> None:
        self._app().log_line("提示：完成配置前无法启动监控（gws + 收件邮箱）")
        self.dismiss(None)

    def action_later(self) -> None:
        self._later(None)


# ---------------------------------------------------------------------------
# 设置（唯一入口：所有配置集中在此）
# ---------------------------------------------------------------------------

class SettingsScreen(ModalScreen[None]):
    BINDINGS = [Binding("escape", "cancel", "取消")]

    def _app(self) -> "CourserApp":
        return self.app  # type: ignore[return-value]

    def compose(self) -> ComposeResult:
        with Vertical(id="settingsbox"):
            yield Label("[bold cyan]设置[/] — 所有配置集中在此处", classes="help-title")
            with VerticalScroll(id="setscroll"):
                yield Label("[bold]账号凭据[/]（可选；留空则依赖浏览器密码管理器自动填充）")
                yield Input(placeholder="学号 / 用户名", id="set_user")
                yield Input(placeholder="密码（留空=自动填充）", id="set_pass", password=True)

                yield Label("[bold]邮件通知[/]（通过 gws 发送，需先 `gws auth login` 授权）")
                yield Static(id="set_gws_status")
                yield Input(placeholder="收件人邮箱（提醒发送到的地址）", id="set_to")
                yield Input(placeholder="gws 发件账号（Gmail 地址，可选）", id="set_gws_from")
                yield Input(placeholder="同课通知冷却（分钟）", id="set_mail_cooldown")

                yield Label("[bold]轮询节奏[/]（自动带随机抖动）")
                yield Input(placeholder="轮询间隔（分钟）", id="set_interval")
                yield Input(placeholder="间隔抖动比例 0~1", id="set_jitter")
                yield Input(placeholder="翻页随机间隔下限（秒）", id="set_pd_min")
                yield Input(placeholder="翻页随机间隔上限（秒）", id="set_pd_max")

                yield Label("[bold]行为[/]")
                yield Input(placeholder="opencli 会话名", id="set_session")
                with Horizontal():
                    yield Select([("后台窗口（不抢焦点）", "background"),
                                  ("前台窗口", "foreground")], id="set_window",
                                 prompt="浏览器窗口模式")
                    yield Select([("任一命中", "any"), ("全部命中", "all")], id="set_match",
                                 prompt="筛选组合")
                with Horizontal():
                    yield Label("每轮强制重新登录（先登出再登录）")
                    yield Switch(id="set_relogin")

                with Horizontal(id="setbtns"):
                    yield Button("📧 发送测试邮件", id="set_testmail", variant="warning")
                    yield Button("💾 保存", id="set_save", variant="success")
                    yield Button("取消", id="set_cancel", variant="default")
        yield Button(" ✕ ", id="set_x", variant="error")

    def on_mount(self) -> None:
        cfg = self._app().cfg
        c, n = cfg.credentials, cfg.notify
        self.query_one("#set_user", Input).value = c.username
        self.query_one("#set_pass", Input).value = c.password
        gws_ok = "✓ 已安装" if notifier.gws_available() else "✗ 未找到 gws 命令"
        self.query_one("#set_gws_status", Static).update(
            f"gws 状态：{gws_ok}（安装后执行 gws auth login 授权）")
        self.query_one("#set_to", Input).value = n.to
        self.query_one("#set_gws_from", Input).value = n.gws_from
        self.query_one("#set_mail_cooldown", Input).value = str(n.min_interval_min)
        self.query_one("#set_interval", Input).value = str(cfg.interval_min)
        self.query_one("#set_jitter", Input).value = str(cfg.interval_jitter)
        self.query_one("#set_pd_min", Input).value = str(cfg.page_delay_min)
        self.query_one("#set_pd_max", Input).value = str(cfg.page_delay_max)
        self.query_one("#set_session", Input).value = cfg.session
        self.query_one("#set_window", Select).value = cfg.window
        self.query_one("#set_match", Select).value = cfg.filters.match
        self.query_one("#set_relogin", Switch).value = cfg.force_relogin

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
        n.min_interval_min = self._float("#set_mail_cooldown", 15.0)
        cfg.interval_min = self._float("#set_interval", 8.0)
        cfg.interval_jitter = self._float("#set_jitter", 0.4)
        cfg.page_delay_min = self._float("#set_pd_min", 6.0)
        cfg.page_delay_max = self._float("#set_pd_max", 14.0)
        cfg.session = self.query_one("#set_session", Input).value.strip() or "courser-watch"
        cfg.window = self.query_one("#set_window", Select).value
        cfg.filters.match = self.query_one("#set_match", Select).value
        cfg.force_relogin = self.query_one("#set_relogin", Switch).value
        cfg.first_run_done = True
        cfg.save()

    @on(Button.Pressed, "#set_save")
    def _save(self, event: Button.Pressed) -> None:
        self._apply()
        self._app().log_line("✔ 设置已保存，首次配置完成")
        self.dismiss(None)
        self._app()._maybe_close_first_run()

    @on(Button.Pressed, "#set_cancel")
    @on(Button.Pressed, "#set_x")
    def _cancel(self, event: Button.Pressed) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#set_testmail")
    def _test_mail(self, event: Button.Pressed) -> None:
        self._apply()
        cfg = self._app().cfg
        self._app().log_line("正在发送测试邮件…")
        ok = notifier.send_email(cfg.notify, "【courser】测试邮件",
                                 "这是 courser 发送的测试邮件。收到说明邮件通知配置正常。",
                                 log=self._app().log_line)
        if ok:
            self._app().notify("测试邮件已发送 ✓", timeout=5)

    def action_cancel(self) -> None:
        self.dismiss(None)


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
    VIEW_NAMES = {"all": "全部课程", "matched": "命中筛选", "seats": "有空余名额"}

    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.courses: list[Course] = []
        self.snapshot_ts: Optional[str] = None
        self.candidate_lists = {"names": [], "categories": [], "depts": []}
        self.view = "all"
        self.watcher: Optional[Watcher] = None
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
        yield Header(show_clock=True)
        with Horizontal(id="menubar"):
            yield Button("⏵ 监控", id="btn_monitor", classes="menu")
            yield Button("⏭ 立即抓取", id="btn_round", classes="menu")
            yield Button("🎯 筛选", id="btn_filter", classes="menu")
            yield Button("⚙ 设置", id="btn_settings", classes="menu")
            yield Button("❓ 帮助", id="btn_help", classes="menu")
        yield Static(id="status")
        with Horizontal(id="body"):
            with Vertical(id="left"):
                yield DataTable(id="table")
                yield RichLog(id="log", highlight=True, markup=False, wrap=True)
            with Vertical(id="right"):
                yield Static(id="filters_panel")
                yield Static(id="mail_panel")
        yield Footer()

    def on_mount(self) -> None:
        self.watcher = Watcher(self.cfg, log=self._thread_log, on_round=self._on_round)
        self._setup_table()
        self.render_table()
        self.set_interval(1.0, self._tick)
        self.log_line(f"启动：筛选 {FilterSet(self.cfg.filters).describe()}")
        self.log_line("菜单：监控 / 筛选 / 设置 / 帮助；快捷键见底部 Footer。")
        self.call_after_refresh(self._maybe_first_run)

    def _maybe_first_run(self) -> None:
        if not self.cfg.first_run_done and not isinstance(self.screen, FirstRunScreen):
            self.push_screen(FirstRunScreen())
            self.log_line("首次使用：请先完成配置（gws + 收件邮箱）")

    def _maybe_close_first_run(self) -> None:
        if isinstance(self.screen, FirstRunScreen) and self.cfg.first_run_done:
            self.pop_screen()

    def _setup_table(self) -> None:
        dt = self.query_one("#table", DataTable)
        dt.add_column("课程号", key="no", width=10)
        dt.add_column("课程名", key="name", width=28)
        dt.add_column("课程类别", key="cat", width=22)
        dt.add_column("开课单位", key="dept", width=16)
        dt.add_column("教师", key="teacher", width=16)
        dt.add_column("限/选", key="seats", width=9)
        dt.add_column("空余", key="avail", width=6)
        dt.add_column("状态", key="status", width=9)

    def render_table(self) -> None:
        dt = self.query_one("#table", DataTable)
        dt.clear()
        fs = FilterSet(self.cfg.filters)
        for c in self.courses:
            matched = fs.matches(c)
            if self.view == "matched" and not matched:
                continue
            if self.view == "seats" and not c.has_seats:
                continue
            seats = Text(f"{c.selected}/{c.quota}" if c.quota is not None else c.seats_raw)
            avail = Text(str(c.avail), style="bold green" if c.has_seats else "dim red")
            name = Text(("★ " if matched else "") + c.name,
                        style="bold" if matched else "default")
            dt.add_row(c.course_no, name, c.category, c.dept, c.teacher,
                       seats, avail, c.status, key=c.key)

    # -- 事件 -------------------------------------------------------------
    @on(Button.Pressed, "#btn_monitor")
    def _btn_monitor(self, event: Button.Pressed) -> None:
        self.action_toggle_monitor()

    @on(Button.Pressed, "#btn_round")
    def _btn_round(self, event: Button.Pressed) -> None:
        self.action_run_round()

    @on(Button.Pressed, "#btn_filter")
    def _btn_filter(self, event: Button.Pressed) -> None:
        self.action_open_filters()

    @on(Button.Pressed, "#btn_settings")
    def _btn_settings(self, event: Button.Pressed) -> None:
        self.action_open_settings()

    @on(Button.Pressed, "#btn_help")
    def _btn_help(self, event: Button.Pressed) -> None:
        self.action_open_help()

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
        run_state = "▶ 监控中" if w.running else "⏸ 未开始"
        countdown = f"{w.countdown_s}s" if w.countdown_s is not None else "--"
        last = ""
        if w.last_result:
            last = (f"{w.last_result.pages}页/{w.last_result.total}课 "
                    f"{w.last_result.duration_s:.0f}s" + (" ✔" if w.last_result.ok else " ✗"))
        risk = "风控 --" if not w.last_result else _risk_text(w.last_result.risk_percent,
                                                             w.last_result.risk_label)
        fs = FilterSet(self.cfg.filters)
        mail = "已配置" if self.cfg.notify.configured else "未配置"
        st.update(
            f"{run_state} | 间隔 {self.cfg.interval_min}min±{int(self.cfg.interval_jitter * 100)}%"
            f" | 下一轮 {countdown} | 上一轮 {last} | {risk} | "
            f"筛选 {fs.describe()} | 邮件 {mail}")
        self.query_one("#btn_monitor", Button).label = "⏸ 停止" if w.running else "⏵ 监控"
        fp = self.query_one("#filters_panel", Static)
        fp.update("[bold]筛选概览[/]\n" + fs.describe() + "\n"
                  f"[dim]视图：{self.VIEW_NAMES[self.view]}（按 v 切换）[/]")
        n = self.cfg.notify
        gws_txt = "gws ✓" if notifier.gws_available() else "gws ✗"
        mp = self.query_one("#mail_panel", Static)
        mp.update("[bold]邮件通知（gws）[/]\n"
                  f"收件人 {n.to or '—'}\n"
                  f"发件 {n.gws_from or '（认证账号）'}\n"
                  + (f"[green]{gws_txt} · 已配置 🎯[/]" if n.configured and notifier.gws_available()
                     else "[red]未配置/未授权 ✗[/]"))

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
            self.log_line("⏸ 监控已停止")
        else:
            w.start()
            self.log_line(f"▶ 监控开始：每约 {self.cfg.interval_min} 分钟一轮（带抖动）")

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
        def on_submit(v: str) -> None:
            try:
                self.cfg.interval_min = max(1.0, float(v))
                self.cfg.save()
                self.log_line(f"轮询间隔已设为 {self.cfg.interval_min} 分钟（带抖动）")
            except ValueError:
                self.log_line("间隔格式错误，未修改")
        self.push_screen(IntervalModal(self.cfg.interval_min, on_submit))

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

    CourserApp(cfg).run()
    return 0


if __name__ == "__main__":
    sys.exit(main())