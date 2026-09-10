"""邮件通知单元测试（不连 Google，mock gws 子进程）。

覆盖：to 为空 / gws 缺失 失败分支；成功发送 + MIME/From 兜底用 profile；
build_body 纯文本/HTML 列齐、含页码、无学分/年级/状态、三类居中。
用法：uv run python tests/unit/test_notification.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from email import message_from_string
from email.header import decode_header

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser import notifier  # noqa: E402
from courser.config import Notify  # noqa: E402
from courser.models import Course  # noqa: E402

_calls: list = []


def _fake_run(cmd, **kwargs):
    _calls.append(cmd)
    if "messages" in cmd and "send" in cmd:
        return types.SimpleNamespace(returncode=0, stdout='{"id":"m"}', stderr="")
    if "getProfile" in cmd:
        return types.SimpleNamespace(returncode=0,
                                     stdout='{"emailAddress":"prof@example.com"}', stderr="")
    return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")


def _enable_fake_gws():
    notifier.shutil.which = lambda n: "/usr/local/bin/gws" if n == "gws" else None
    notifier.subprocess.run = _fake_run


def _dh(v):
    return "".join(t.decode(charset or "utf-8") if isinstance(t, bytes) else t
                   for t, charset in decode_header(v))


def _mk(**kw) -> Course:
    base = dict(course_no="02030330", name="民俗学", category="通识课(通识核心课III)",
                dept="英语语言文学系", teacher="王娟(教授)", class_no="1",
                quota=150, selected=149, avail=1, status="可申请", seq="BZ202402030330_1",
                page=1, schedule="1~16周 每周周二5~6节")
    base.update(kw)
    return Course(**base)


def test_failure_branches():
    assert not notifier.send_email(Notify(to=""), "s", "b"), "to 为空应失败"
    real = notifier.shutil.which
    notifier.shutil.which = lambda _: None
    assert not notifier.send_email(Notify(to="x@y.z"), "s", "b"), "gws 缺失应失败"
    notifier.shutil.which = real
    print("✓ notifier：to 为空 / gws 缺失 失败分支")


def test_gws_install_hint():
    assert notifier.GWS_INSTALL_COMMAND == "npm install -g @googleworkspace/cli"
    assert notifier.GWS_SETUP_COMMAND == "gws auth setup"
    assert notifier.GWS_LOGIN_COMMAND == "gws auth login"
    assert "brew" not in notifier._AUTH_HINT.lower()
    # 安装流程闭环：首次必须走 setup→login，不能退化成单独提示 gws auth login
    assert "gws auth setup" in notifier._AUTH_HINT
    assert notifier.AUTH_STEPS.startswith(notifier.GWS_SETUP_COMMAND), \
        "首次授权应引导先 gws auth setup 再 gws auth login"
    assert notifier.GWS_LOGIN_COMMAND in notifier._AUTH_HINT


def test_send_and_profile():
    _enable_fake_gws()
    _calls.clear()
    ok = notifier.send_email(Notify(to="you@example.com"), "主题", "正文")
    assert ok, "mock gws 应发送成功"
    assert notifier._gws_profile_email() == "prof@example.com", "From 兜底用 profile 邮箱"
    print("✓ notifier：成功发送、profile 邮箱兜底")


def test_build_body_columns():
    c2 = _mk(course_no="P2", name="A课(第2页)", category="通识课(通识核心课III)",
             dept="外国语学院", page=2, quota=50, selected=49)
    c1 = _mk(course_no="P1", name="B课(第1页)", page=1, quota=50, selected=49, avail=1)
    text, html_body = notifier.build_body([c2, c1], "ts")
    # 邮件按选课网顺序稳定排序：页码小的在前。
    assert text.find("P1") < text.find("P2")
    assert "（第 2 页）" in text and "（第 1 页）" in text
    assert html_body.find("P1") < html_body.find("P2")
    assert ">2</td>" in html_body and ">1</td>" in html_body
    assert "按选课网顺序" not in text and "按选课网顺序" not in html_body

    same_a = _mk(course_no="S1", name="同页上方", page=1)
    same_b = _mk(course_no="S2", name="同页下方", page=1)
    same_text, _ = notifier.build_body([same_a, same_b], "ts")
    assert same_text.find("S1") < same_text.find("S2"), \
        "同页应保持抓取到的上/下顺序"

    # 表格列与样式
    for col in ["页数", "课程号", "课程名", "课程类别", "教师", "班号", "开课单位",
                "上课/考试信息", "限选", "已选", "空余"]:
        assert col in html_body, f"缺少列 {col}"
    for banned in ["学分", "周学时", "年级", "选课状态", "自选P/NP", "状态", "限数/已选"]:
        assert banned not in html_body, f"不应包含 {banned}"
    # 居中：页/类别/限选/已选 各 th+td×2行 → header 4 + 2 行×4 = 12
    assert html_body.count('style="text-align:center;"') == 12
    assert html_body.count("<th>") + html_body.count('<th style="text-align:center;">') == 11
    # 单门课：header 4 + 1 行×4 = 8
    _, single_html = notifier.build_body([_mk()], "ts")
    assert single_html.count('style="text-align:center;"') == 8, single_html
    print("✓ build_body：含页码、顺序保留、11 列、四类居中、无学分/年级/状态")


def _decode_mime(raw: str) -> str:
    padded = raw + "=" * (-len(raw) % 4)
    return base64.urlsafe_b64decode(padded).decode("utf-8")


def test_send_mime():
    _enable_fake_gws()
    _calls.clear()
    ok = notifier.send_email(
        Notify(to="you@example.com", gws_from="sender@example.com"),
        "【选课提醒】补退选有空余名额：民俗学", "正文",
        body_html="<html><body>h</body></html>", log=lambda m: None)
    assert ok
    send_cmds = [c for c in _calls if "messages" in c and "send" in c]
    assert send_cmds, "应产生 gws send 命令"
    cmd = send_cmds[0]
    assert cmd[0] == "gws" and cmd[2] == "users" and cmd[3] == "messages", cmd
    payload = json.loads(cmd[cmd.index("--json") + 1])
    mime_text = _decode_mime(payload["raw"])
    msg = message_from_string(mime_text)
    assert msg["From"] == "sender@example.com"
    assert msg["To"] == "you@example.com"
    assert _dh(msg["Subject"]) == "【选课提醒】补退选有空余名额：民俗学", msg["Subject"]
    # 必须含 text/html 替代部分
    assert any(p.get_content_type() == "text/html" for p in msg.walk())
    print("✓ MIME：gws 命令、From/To/Subject、text/html 替代部分正确")


def test_send_retry_and_status():
    """发信失败自动重试（3次×间隔）并把最近一次结果记录为连通性状态。"""
    _enable_fake_gws()
    calls = {"send": 0}

    def controlled(cmd, **kw):
        if "messages" in cmd and "send" in cmd:
            calls["send"] += 1
            if calls["send"] < 3:
                return types.SimpleNamespace(returncode=2, stdout="", stderr="auth fail")
            return types.SimpleNamespace(returncode=0, stdout='{"id":"m"}', stderr="")
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")

    notifier.subprocess.run = controlled
    ok = notifier.send_email_with_retry(Notify(to="a@b.c", gws_from="g@x.com"),
                                        "s", "b", attempts=3, delay_s=0.01)
    assert ok, "前两次失败、第三次成功应最终成功"
    assert calls["send"] == 3, f"应尝试 3 次，实际 {calls['send']}"
    res, _ts = notifier.last_mail_status()
    assert res is True, "最终成功应记录为可达"

    # 全部失败 → 尝试 3 次、记录为不可达、返回 False
    def always_fail(cmd, **kw):
        if "messages" in cmd and "send" in cmd:
            calls["send"] += 1
            return types.SimpleNamespace(returncode=2, stdout="", stderr="down")
        return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")

    calls["send"] = 0
    notifier.subprocess.run = always_fail
    ok2 = notifier.send_email_with_retry(Notify(to="a@b.c", gws_from="g@x.com"),
                                         "s", "b", attempts=3, delay_s=0.01)
    assert not ok2 and calls["send"] == 3, "全部失败应返回 False 且恰好尝试 3 次"
    res2, _ = notifier.last_mail_status()
    assert res2 is False, "全部失败应记录为不可达"
    print("✓ 发信重试：失败-失败-成功→成功；全失败→失败并更新状况")


def main() -> int:
    test_failure_branches()
    test_send_and_profile()
    test_build_body_columns()
    test_send_mime()
    test_send_retry_and_status()
    print("=" * 60)
    print("notifier 单元测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
