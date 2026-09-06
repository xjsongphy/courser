"""邮件通知。

推荐方式：Gmail「应用专用密码」(App Password) + SMTP —— 无需额外命令行工具，
Python 标准库 smtplib 即可（要求 Gmail 开启两步验证后生成 16 位应用密码）。
发件/收件、服务器均可配置，也可换成任意支持 SMTP 的邮箱（如 163/QQ 开 SMTP 服务）。
"""

from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Callable, Optional

from .config import Notify

_MISSING_HINT = (
    "邮件未配置或配置不完整：需要 收件人(to)、发件账号(smtp_user)、"
    "应用专用密码(smtp_pass)。Gmail 请先开启两步验证，再在 "
    "https://myaccount.google.com/apppasswords 生成 16 位应用密码，"
    "填入「设置 → 邮件通知」（或 .env 的 SMTP_USER/SMTP_PASS）。"
)


def send_email(notify: Notify, subject: str, body: str,
               log: Optional[Callable[[str], None]] = None) -> bool:
    if not notify.configured:
        msg = _MISSING_HINT
        if log:
            log(msg)
        else:
            print("[notifier]", msg)
        return False

    msg = EmailMessage()
    msg["From"] = notify.smtp_user
    msg["To"] = notify.to
    msg["Subject"] = subject
    msg.set_content(body)

    try:
        if notify.smtp_port == 587:
            with smtplib.SMTP(notify.smtp_host, notify.smtp_port, timeout=30) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.login(notify.smtp_user, notify.smtp_pass)
                s.send_message(msg)
        else:
            with smtplib.SMTP_SSL(notify.smtp_host, notify.smtp_port, timeout=30,
                                  context=ssl.create_default_context()) as s:
                s.login(notify.smtp_user, notify.smtp_pass)
                s.send_message(msg)
        if log:
            log(f"邮件已发送 → {notify.to} 主题：{subject}")
        return True
    except Exception as exc:  # noqa: BLE001
        if log:
            log(f"邮件发送失败：{exc}")
        else:
            print("[notifier] 邮件发送失败:", exc)
        return False


def build_body(courses: list, ts: str) -> str:
    lines = [
        f"补退选时空余名额提醒（{ts}）",
        "",
        "以下课程符合你的筛选条件，且当前有空余名额：",
        "",
    ]
    for c in courses:
        seats = f"{c.selected}/{c.quota}（空余 {c.avail}）" if c.quota is not None else c.seats_raw
        lines.append(f"• {c.name} [{c.course_no}]")
        lines.append(f"    类别：{c.category}    开课单位：{c.dept}")
        lines.append(f"    教师：{c.teacher}    限数/已选：{seats}    状态：{c.status or '—'}")
        if c.schedule:
            lines.append(f"    时间：{c.schedule}")
        lines.append("")
    lines.append("请尽快登录选课系统操作：")
    lines.append("http://elective.pku.edu.cn/elective2008/")
    lines.append("")
    lines.append("（本邮件由 courser 自动发送）")
    return "\n".join(lines)


def build_subject(courses: list) -> str:
    names = "、".join(c.name for c in courses[:3])
    if len(courses) > 3:
        names += f" 等{len(courses)}门"
    return f"【选课提醒】补退选有空余名额：{names}"