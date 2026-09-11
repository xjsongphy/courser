"""统一排版原语（typographic primitives）+ 样式 + 常量 + 纯文本工具。

全应用只许用这套词汇，各 renderer 不得各自乱造 [dim]/[bold]/[cyan]。
   层级：页面标题 > 区块标题 > value(正文) > label/meta(退后)
   cyan 只表示交互/焦点/当前选择；普通数据一律用默认前景。
  语义色：green=成功，yellow=警告，red=失败。
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.markup import escape
from rich.text import Text
from textual.containers import VerticalScroll
from textual.widgets import Static

from ..config import Config

SEP = "[dim] · [/]"  # 行内弱分隔符，自动退到背景

# Courser 统一强调色（identity / interaction）：暗色终端下不刺眼的蓝。
# 只表示品牌 + 交互焦点；成功/警告/错误等语义色不染蓝。
ACCENT = "#58A6FF"


def ui_accent(text: str) -> str:
    return f"[{ACCENT}]{escape(text)}[/]"


def ui_title(text: str) -> str:
    return f"[bold {ACCENT}]{escape(text)}[/]"


def ui_section(text: str) -> str:
    return f"[bold]{escape(text)}[/]"


def ui_label(text: str) -> str:
    return f"[dim]{escape(text)}[/]"


def ui_value(text: str) -> str:
    return escape(text)


def ui_meta(text: str) -> str:
    return f"[dim]{escape(text)}[/]"


def ui_key(text: str) -> str:
    return f"[{ACCENT}]{escape(text)}[/]"


def ui_ok(text: str) -> str:
    return f"[green]{escape(text)}[/]"


def ui_warn(text: str) -> str:
    return f"[yellow]{escape(text)}[/]"


def ui_error(text: str) -> str:
    return f"[red]{escape(text)}[/]"


def _hint(*pairs: tuple[str, str]) -> str:
    """由 (键, 说明) 拼快捷键行：键用交互色，说明退后为次要。"""
    return SEP.join(f"{ui_key(k)} {ui_meta(d)}" for k, d in pairs)


# 风控风险度 → 颜色（content 语义部分，不含外部 label）
_RISK_COLOR = {"高": "red", "极高": "red", "中": "yellow",
               "已触发/疑似": "red"}


def risk_markup(percent: int, label: str) -> str:
    if label == "无" or percent == 0:
        return ""
    color = _RISK_COLOR.get(label, "yellow")
    return (f"[dim]⚠[/] [{color}]{percent}%[/] "
            f"[dim]{escape(label)}[/]")


# 筛选三维度：[配置字段名, 中文名]
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
    ("邮件通知（gws 发送，需先 `gws auth setup` 后 `gws auth login` 授权）", [
        {"key": "to", "label": "收件邮箱", "kind": "text"},
        {"key": "gws_from", "label": "gws 发件账号", "kind": "text"},
        {"key": "max_per_hour", "label": "每小时最多发送", "kind": "int"},
        {"key": "min_interval_min", "label": "同课通知冷却（分）", "kind": "float"},
        {"key": "test_mail", "label": "发送一封测试邮件", "kind": "action"},
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
        {"key": "night_pause", "label": "夜间暂停", "kind": "enum",
         "opts": [("false", "关"), ("true", "开")],
         "note": "0 点至 6 点暂停抓取"},
        {"key": "random_break", "label": "随机暂停", "kind": "enum",
         "opts": [("off", "关"), ("light", "轻"), ("medium", "中"), ("strong", "强")]},
    ]),
]

CSS = """
Screen { background: transparent; }
Vertical, VerticalScroll, Static { background: transparent; }
VerticalScroll:focus { border: none; }

/* 主页顶部产品身份 + 稳定摘要（Hero）：Rich Panel 负责边框，Textual 只管安排位置。 */
/* brand 不再兼任键盘焦点锚（_anchor_focus 改为 set_focus(None)）。 */
#hero { height: auto; padding: 0 2; margin-bottom: 1; }
#stage { height: 1fr; min-height: 0; padding: 0 2; }

/* 主页 */
#page-main { height: 1fr; min-height: 0; padding: 0 2; border: none;
             overflow: hidden; }
#courselist { height: 1fr; min-height: 1; margin: 0 0 1 0; overflow: hidden; }
#coursehead { height: auto; margin: 0 0 1 0; }

/* 筛选的候选列表与设置项相同：由真实 viewport 滚动，光标绝不越出页面。 */
#page-filters {
    height: 1fr;
    min-height: 0;
    padding: 1 2;
    border: none;
    overflow: hidden;
}

/* 设置页：正文进入可滚动 viewport（内容再长也不能把底部 #keys/#activity 挤出屏幕） */
#page-settings {
    height: 1fr;
    min-height: 0;
    padding: 1 2;
    border: none;
    overflow: hidden;
}

/* 次级页面：铺开成终端文本，不套 GUI 面板；#keys 折行变高时要能随之收缩 */
#page-logs, #page-help, #page-detail, #page-setup {
    height: 1fr;
    min-height: 0;
    padding: 1 2;
    border: none;
}

#filtersscroll, #logscroll, #helpscroll, #detscroll, #settingsscroll { height: 1fr; }
#filters_list, #settings_list, #setupbody { height: auto; }

/* TUI 滚动条：1 格灰色 thumb，track 透明，不出现彩色 hover */
FocusScroll {
    overflow-x: hidden;
    overflow-y: auto;
    scrollbar-size-horizontal: 0;
    scrollbar-size-vertical: 1;
    scrollbar-background: transparent;
    scrollbar-background-hover: transparent;
    scrollbar-background-active: transparent;
    scrollbar-color: #555555;
    scrollbar-color-hover: #666666;
    scrollbar-color-active: #777777;
    scrollbar-corner-color: transparent;
}

/* 底部操作提示：窗口窄时自动折行（多行），不再把右侧裁掉。
   高度自适应但设上限+内部滚动，避免极窄窗把上方正文挤没。 */
#keys {
    height: auto;
    max-height: 4;
    padding: 0 4;
    text-wrap: wrap;
    overflow-x: hidden;
    overflow-y: auto;
}

/* 底部的全局活动状态行：固定一行，左对齐到主内容区；
   未着色用默认前景，状态词按语义染色，次要信息由 dim 承担。 */
#activity {
    height: 1;
    min-height: 1;
    padding: 0 4;
    margin: 0 0 1 0;   /* 状态栏不贴底：与上方提示行的留白（#keys padding-bottom 1）对称 */
}

/* 复制成功提示：overlay 不占布局、不移动 footer / viewport，极简一行自动消失 */
ToastRack {
    align: center bottom;
    padding: 0 0 2 0;
}

Toast {
    width: auto;
    height: 1;
    min-height: 1;
    padding: 0 2;
    border: none;
    background: #202020;
    color: #cccccc;
}

Toast .toast--title {
    display: none;
}
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


def kv_row(label: str, value: str, width: int = 14, prefix: str = "  ") -> str:
    """统一的固定标签列；value 可带 Rich markup。"""
    return f"{prefix}{ui_label(_pad(label, width))}  {value}"


def shortcut_row(key: str, description: str, width: int = 10) -> str:
    """统一的快捷键列；避免页面 renderer 用手工空格猜列宽。"""
    return f"  {ui_key(_pad(key, width))}  {ui_value(description)}"


def page_hint(text: str = "↑↓ / PgUp / PgDn 滚动 · Esc 返回") -> str:
    return ui_meta(text)


def plain_markup(text: str) -> str:
    """把内部日志中的可选 Rich markup 安全还原成终端可读纯文本。"""
    try:
        return Text.from_markup(text).plain
    except Exception:
        return text


def field_value(cfg: Config, key: str) -> str:
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
    if key == "night_pause":
        return "true" if cfg.night_pause else "false"
    if key == "random_break":
        return str(cfg.random_break or "off")
    return str(getattr(cfg, key, ""))


def field_mutate(cfg: Config, key: str, value: str) -> None:
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
    elif key == "night_pause":
        cfg.night_pause = value == "true"
    elif key == "random_break":
        cfg.random_break = value if value in ("off", "light", "medium", "strong") else "off"
    elif key == "session":
        cfg.session = value
    elif key == "window":
        cfg.window = value
    else:
        setattr(cfg, key, float(value))
