"""邮件通知：通过 gws（Google Workspace CLI）发送。

gws = https://github.com/googleworkspace/cli（npm 包 @googleworkspace/cli）。
安装并完成授权：

    npm install -g @googleworkspace/cli
    gws auth setup          # 首次：初始化 Google Cloud 项目 / OAuth 配置 / 启用 API
    gws auth login          # 首次及后续：浏览器完成 OAuth2 授权

本模块组装 RFC822 邮件 → base64url → 调 Gmail API users.messages.send。
不依赖 smtplib，也不要求填写 SMTP 密码；发件账号由 gws 认证账号提供
（也可在「设置」里指定 gws_from）。
"""

from __future__ import annotations

import base64
import html
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Callable, Optional

from .config import Notify
from .course_table import column, course_display, ordered_courses, seats_display

_GWS = "gws"
GWS_INSTALL_COMMAND = "npm install -g @googleworkspace/cli"
GWS_SETUP_COMMAND = "gws auth setup"      # 一次性：初始化项目/OAuth/启用 API
GWS_LOGIN_COMMAND = "gws auth login"      # 首次及后续：浏览器授权 Gmail
AUTH_STEPS = f"{GWS_SETUP_COMMAND}，再执行 {GWS_LOGIN_COMMAND}"
_AUTH_HINT = (f"请先配置 gws：安装 {GWS_INSTALL_COMMAND}，然后执行 {AUTH_STEPS} 完成授权；"
              f"（token 失效时重新执行 {GWS_LOGIN_COMMAND}）"
              "然后在 courser「设置」中填写 收件邮箱（gws 发件账号可选）。")

# 最近一次发信结果（用于 TUI 展示 Google/邮件连通性）
#   None = 尚未尝试发信；True = 最近一次成功；False = 最近一次失败。
_last_mail_result: Optional[bool] = None
_last_mail_at: float = 0.0


def gws_available() -> bool:
    return shutil.which(_GWS) is not None


@dataclass
class GwsStatus:
    """gws 授权状态模型。

    auth: ready（已授权）/ invalid（token 失效）/ missing（未登录）/ unknown（探测失败）
    account: 认证账号邮箱（可用时）。
    """

    auth: str = "unknown"
    account: Optional[str] = None


def gws_auth_status(timeout: float = 20.0) -> GwsStatus:
    """真正探测 gws 授权状态：跑 `gws auth status` 并解析 token_valid / user。

    不再拿 `_gws_profile_email` 当授权探针——它调用的是 Gmail 只读 profile，
    与纯 `gmail.send` scope 并不等价，且不区分"未登录"与"token 失效"。
    """
    try:
        proc = subprocess.run([_GWS, "auth", "status"],
                              capture_output=True, text=True, timeout=timeout)
    except Exception:
        return GwsStatus(auth="unknown")
    if proc.returncode != 0:
        out = (proc.stdout or "") + (proc.stderr or "")
        return GwsStatus(auth="missing" if ("login" in out or "not" in out.lower()) else "unknown")
    try:
        d = json.loads(proc.stdout or "{}")
    except Exception:
        return GwsStatus(auth="unknown")
    valid = bool(d.get("token_valid"))
    account = str(d.get("user") or "").strip() or None
    if valid:
        return GwsStatus(auth="ready", account=account)
    has_creds = bool(d.get("token_cache_exists")
                     or d.get("plain_credentials_exists")
                     or d.get("encrypted_credentials_exists"))
    return GwsStatus(auth="invalid" if has_creds else "missing", account=account)


# 授权状态缓存（避免每次渲染都起 subprocess）；60s TTL，足够短不至于误导。
_AUTH_TTL = 60.0
_auth_cache: Optional[GwsStatus] = None
_auth_cache_at: float = 0.0
_auth_lock = threading.Lock()


def gws_auth_status_cached(ttl: float = _AUTH_TTL) -> GwsStatus:
    """带缓存的授权状态；首次调用起 subprocess，TTL 内直接复用。"""
    global _auth_cache, _auth_cache_at
    now = time.time()
    with _auth_lock:
        if _auth_cache is not None and (now - _auth_cache_at) < ttl:
            return _auth_cache
        st = gws_auth_status()
        _auth_cache, _auth_cache_at = st, now
        return st


def _gws_profile_email(log: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """读取 gws 认证账号的邮箱（只读接口，失败返回 None）。"""
    try:
        proc = subprocess.run(
            [_GWS, "gmail", "users", "getProfile", "--params", '{"userId":"me"}'],
            capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            return json.loads(proc.stdout).get("emailAddress")
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"读取 gws 账号邮箱失败：{exc}")
    return None


def send_email(notify: Notify, subject: str, body: str,
               body_html: Optional[str] = None,
               log: Optional[Callable[[str], None]] = None) -> bool:
    if not notify.to:
        if log:
            log("邮件未配置：请在「设置 → 邮件通知」填写 收件邮箱。")
        return False
    if not gws_available():
        if log:
            log("未找到 gws 命令：\n" + _AUTH_HINT)
        return False

    from_addr = notify.gws_from or _gws_profile_email(log) or "me"

    mime = EmailMessage()
    mime["From"] = from_addr
    mime["To"] = notify.to
    mime["Subject"] = subject
    mime.set_content(body)
    if body_html:
        mime.add_alternative(body_html, subtype="html")
    raw = base64.urlsafe_b64encode(mime.as_bytes()).decode("ascii").rstrip("=")

    cmd = [_GWS, "gmail", "users", "messages", "send",
           "--params", '{"userId":"me"}', "--json", json.dumps({"raw": raw})]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        out, err = (proc.stdout or "").strip(), (proc.stderr or "").strip()
        if proc.returncode == 0:
            try:
                mid = json.loads(out).get("id", "?")
            except Exception:  # noqa: BLE001
                mid = "?"
            if log:
                log(f"邮件已发送 → {notify.to}（gws messageId={mid}）主题：{subject}")
            return True
        if log:
            log(f"gws 发送失败（exit={proc.returncode}）：{err[:300] or out[:300]}\n"
                f"若提示未授权，请执行：{GWS_LOGIN_COMMAND}")
        return False
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"gws 调用异常：{exc}")
        return False


def build_body(courses: list, ts: str) -> tuple[str, str]:
    """返回 (纯文本正文, HTML 正文)。HTML 为表格样式，仿选课网列；
    明确不含「状态 / 自选P/NP」（P/NP 是否可申请不随邮件发送）。"""
    return _plain_body(courses, ts), _html_body(courses, ts)


def _plain_body(courses: list, ts: str) -> str:
    lines = [f"补退选时空余名额提醒（{ts}）", "",
             "以下课程符合你的筛选条件，且当前有空余名额：", ""]
    for c in ordered_courses(courses):
        seats = seats_display(c)
        page = f"（第 {c.page} 页）" if c.page else ""
        lines.append(f"• {c.name} [{c.course_no}] {page}")
        lines.append(f"    课程类别：{c.category}    开课单位：{c.dept}")
        lines.append(f"    教师：{c.teacher}    限/已选：{seats}    空余：{course_display(c, 'avail')}")
        if c.schedule:
            lines.append(f"    上课/考试信息：{c.schedule}")
        lines.append("")
    lines.append("请尽快登录选课系统操作：http://elective.pku.edu.cn/elective2008/")
    lines.append("")
    lines.append("（本邮件由 courser 自动发送）")
    return "\n".join(lines)


def _html_body(courses: list, ts: str) -> str:
    esc = lambda s: html.escape(s or "", quote=True)
    page_title = column("page").title

    def td(value: str, extra: str = "") -> str:
        return f"<td{extra}>{esc(value)}</td>"

    rows = []
    for c in ordered_courses(courses):
        page = course_display(c, "page")
        rows.append("<tr>" + "".join([
            td(page, ' style="text-align:center;"'),
            td(course_display(c, "no")),
            td(course_display(c, "name")),
            td(course_display(c, "cat"), ' style="text-align:center;"'),
            td(course_display(c, "teacher")),
            td(course_display(c, "class_no")),
            td(course_display(c, "dept")),
            td(course_display(c, "schedule"), ' style="max-width:260px;word-break:break-all;"'),
            td(course_display(c, "quota"), ' style="text-align:center;"'),
            td(course_display(c, "selected"), ' style="text-align:center;"'),
            td(course_display(c, "avail"), ' style="color:#c0392b;font-weight:bold;text-align:center;"'),
        ]) + "</tr>")
    return (
        "<html><body style=\"font-family:Helvetica,Arial,'PingFang SC','Microsoft YaHei',sans-serif;"
        "font-size:14px;color:#222;\">"
        f"<p>补退选时空余名额提醒（{esc(ts)}）</p>"
        f"<p>以下 {len(courses)} 门课程符合筛选条件且当前有空余名额：</p>"
        "<table border=\"1\" cellspacing=\"0\" cellpadding=\"6\" "
        "style=\"border-collapse:collapse;border-color:#ccc;\">"
        "<thead><tr style=\"background:#eef2f8;\">"
        f"<th style=\"text-align:center;\">{esc(page_title)}</th>"
        "<th>课程号</th><th>课程名</th>"
        "<th style=\"text-align:center;\">课程类别</th>"
        "<th>教师</th><th>班号</th><th>开课单位</th>"
        "<th>上课/考试信息</th>"
        "<th style=\"text-align:center;\">限选</th><th style=\"text-align:center;\">已选</th><th>空余</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        "<p>请尽快登录选课系统操作：<a href=\"http://elective.pku.edu.cn/elective2008/\">"
        "elective.pku.edu.cn</a></p>"
        "<p style=\"color:#888;font-size:12px;\">（本邮件由 courser 自动发送）</p>"
        "</body></html>"
    )


def build_subject(courses: list) -> str:
    courses = ordered_courses(courses)
    names = "、".join(c.name for c in courses[:3])
    if len(courses) > 3:
        names += f" 等{len(courses)}门"
    return f"【选课提醒】补退选有空余名额：{names}"


# ---------------------------------------------------------------------------
# 发信结果状态（TUI 展示 Google/邮件连通性用）
# ---------------------------------------------------------------------------

def last_mail_status() -> tuple[Optional[bool], float]:
    """最近一次发信结果：返回 (None=未发过 / True=成功 / False=失败, 时间戳)。"""
    return _last_mail_result, _last_mail_at


def _record_mail_result(ok: bool) -> None:
    global _last_mail_result, _last_mail_at
    _last_mail_result = ok
    _last_mail_at = time.time()


def send_email_with_retry(notify: Notify, subject: str, body: str,
                          body_html: Optional[str] = None,
                          log: Optional[Callable[[str], None]] = None,
                          attempts: int = 3, delay_s: float = 5.0) -> bool:
    """发信失败自动重试：默认最多 3 次、间隔 5s（硬编码，不进设置）。

    每次尝试都更新最近发信结果状态（供 TUI 连通性展示）；任一次成功即返回 True，
    全部失败返回 False。
    """
    for attempt in range(1, attempts + 1):
        if send_email(notify, subject, body, body_html=body_html, log=log):
            _record_mail_result(True)
            return True
        _record_mail_result(False)
        if attempt < attempts:
            if log:
                log(f"邮件发送失败（第 {attempt}/{attempts} 次），重试前等待 {delay_s:.0f} 秒…")
            time.sleep(delay_s)
    return False
