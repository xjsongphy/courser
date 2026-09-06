"""邮件通知：通过 gws（Google Workspace CLI）发送。

gws = https://github.com/googleworkspace/cli（npm 包 @googleworkspace/cli，
本机 brew 安装于 /opt/homebrew/bin/gws）。gws 需由用户自行安装并完成授权：

    gws auth login        # 浏览器完成 OAuth2 授权

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
from email.message import EmailMessage
from typing import Callable, Optional

from .config import Notify

_GWS = "gws"
_AUTH_HINT = ("请先配置 gws：安装 googleworkspace/cli 并执行 `gws auth login` 完成授权；"
              "然后在 courser「设置」中填写 收件邮箱（gws 发件账号可选）。")


def gws_available() -> bool:
    return shutil.which(_GWS) is not None


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
                f"若提示未授权，请执行：gws auth login")
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
    for c in courses:
        seats = f"{c.selected}/{c.quota}（空余 {c.avail}）" if c.quota is not None else c.seats_raw
        lines.append(f"• {c.name} [{c.course_no}]")
        lines.append(f"    课程类别：{c.category}    开课单位：{c.dept}")
        lines.append(f"    教师：{c.teacher}    限数/已选：{seats}")
        if c.schedule:
            lines.append(f"    上课/考试信息：{c.schedule}")
        lines.append("")
    lines.append("请尽快登录选课系统操作：http://elective.pku.edu.cn/elective2008/")
    lines.append("")
    lines.append("（本邮件由 courser 自动发送）")
    return "\n".join(lines)


def _html_body(courses: list, ts: str) -> str:
    esc = lambda s: html.escape(s or "", quote=True)

    def td(value: str, extra: str = "") -> str:
        return f"<td{extra}>{esc(value)}</td>"

    rows = []
    for c in courses:
        seats = f"{c.selected}/{c.quota}" if c.quota is not None else c.seats_raw
        avail = str(c.avail) if c.avail >= 0 else "—"
        rows.append("<tr>" + "".join([
            td(c.course_no),
            td(c.name),
            td(c.category),
            td(c.credits),
            td(c.weekly_hours),
            td(c.teacher),
            td(c.class_no),
            td(c.dept),
            td(c.grade),
            td(c.schedule, ' style="max-width:260px;word-break:break-all;"'),
            td(seats),
            td(avail, ' style="color:#c0392b;font-weight:bold;text-align:center;"'),
        ]) + "</tr>")
    return (
        "<html><body style=\"font-family:Helvetica,Arial,'PingFang SC','Microsoft YaHei',sans-serif;"
        "font-size:14px;color:#222;\">"
        f"<p>补退选时空余名额提醒（{esc(ts)}）</p>"
        f"<p>以下 {len(courses)} 门课程符合筛选条件且当前有空余名额：</p>"
        "<table border=\"1\" cellspacing=\"0\" cellpadding=\"6\" "
        "style=\"border-collapse:collapse;border-color:#ccc;\">"
        "<thead><tr style=\"background:#eef2f8;\">"
        "<th>课程号</th><th>课程名</th><th>课程类别</th><th>学分</th><th>周学时</th>"
        "<th>教师</th><th>班号</th><th>开课单位</th><th>年级</th>"
        "<th>上课/考试信息</th><th>限数/已选</th><th>空余</th>"
        "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        "<p>请尽快登录选课系统操作：<a href=\"http://elective.pku.edu.cn/elective2008/\">"
        "elective.pku.edu.cn</a></p>"
        "<p style=\"color:#888;font-size:12px;\">（本邮件由 courser 自动发送）</p>"
        "</body></html>"
    )


def build_subject(courses: list) -> str:
    names = "、".join(c.name for c in courses[:3])
    if len(courses) > 3:
        names += f" 等{len(courses)}门"
    return f"【选课提醒】补退选有空余名额：{names}"