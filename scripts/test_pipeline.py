"""全链路自动化测试：解析 → 判断 → 通知决策（冷却/预算） → 发送。

覆盖"从解析到自动发送"的所有关键代码：
1. 页面解析   fetch._parse_page（模拟真实页面提取 JSON，含 14 列）
2. 筛选判断   filters.FilterSet（任一/全部/空筛选）
3. 通知决策   watcher._should_notify（同课冷却）+ _budget_ok（每小时发送上限）
4. 邮件发送   notifier.send_email → 断言 gws 命令、MIME 内容、
             表格列（无学分/周学时/年级/状态）、课程类别与限数/已选居中

不连浏览器、不发真实邮件（mock gws 子进程）。
用法：uv run python scripts/test_pipeline.py
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 使用临时配置与临时日志，避免测试污染真实 config.json / data/courser.log
import tempfile as _tf  # noqa: E402

_tmpenv = Path(_tf.mkdtemp(prefix="courser-tpipe-"))
os.environ["COURSER_CONFIG"] = str(_tmpenv / "config.json")
os.environ["COURSER_LOG"] = str(_tmpenv / "courser.log")

import courser.watcher as W  # noqa: E402
from courser import fetch, notifier  # noqa: E402
from courser.fetch import Course  # noqa: E402
from courser.config import Config, Filters  # noqa: E402
from courser.filters import FilterSet  # noqa: E402

# 与真实 EXTRACT_JS 输出同构的页面数据（14 列，含学分/周学时/年级/状态）
_PAGE = {
    "url": "https://elective.pku.edu.cn/elective2008/edu/pku/stu/elective/controller/supplement/supplement.jsp?netui_row=electableListGrid;0",
    "pager": {"cur": 1, "total": 2},
    "has_next": True,
    "next_href": "/elective2008/edu/pku/stu/elective/controller/supplement/supplement.jsp?netui_pagesize=electableListGrid%3B20&xh=2300011524&netui_row=electableListGrid%3B20",
    "warning": False,
    "tables": [{
        "header": ["课程号", "课程名", "课程类别", "学分", "周学时", "教师",
                   "班号", "开课单位", "年级", "上课/考试信息",
                   "自选P/NP", "限数/已选", "选课状态", "退选"],
        "rows": [
            {"cells": ["00433328", "近代物理实验 (II)", "通识课(通选课III)", "3.0",
                       "6.0", "蒋莹莹(副教授)", "8", "物理学院", "2024",
                       "1~16周 每周周一5~6节", "可申请", "12 / 12", "不可申请", ""],
             "links": [
                 {"t": "近代物理实验 (II)",
                  "h": "/elective2008/edu/pku/stu/elective/controller/supplement/goNested.do?course_seq_no=BZ202400433328_1"},
                 {"t": "补选",
                  "h": "/elective2008/edu/pku/stu/elective/controller/supplement/electSupplement.do?index=0&seq=BZ202400433328_1"}]},
            {"cells": ["02030330", "民俗学", "通识课(通识核心课III)", "2.0",
                       "2.0", "王娟(教授)", "1", "中国语言文学系", "",
                       "1~16周 每周周二5~6节", "可申请", "150 / 149", "可申请", ""],
             "links": [
                 {"t": "民俗学",
                  "h": "/elective2008/edu/pku/stu/elective/controller/supplement/goNested.do?course_seq_no=BZ202402030330_1"}]},
        ],
    }],
}

_calls: list = []


def _fake_run(cmd, **kwargs):
    _calls.append(cmd)
    if "messages" in cmd and "send" in cmd:
        return SimpleNamespace(returncode=0, stdout='{"id":"test-mid"}', stderr="")
    return SimpleNamespace(returncode=0, stdout="{}", stderr="")


def test_parse():
    courses, pager, warned = fetch._parse_page(_PAGE)
    assert len(courses) == 2, f"应解析出 2 门课，实得 {len(courses)}"
    c0, c1 = courses
    assert c0.course_no == "00433328" and c0.name == "近代物理实验 (II)"
    assert c0.category == "通识课(通选课III)" and c0.dept == "物理学院"
    assert c0.quota == 12 and c0.selected == 12 and c0.avail == 0, c0.seats_raw
    assert c0.status == "不可申请" and c0.seq == "BZ202400433328_1"
    assert c1.quota == 150 and c1.selected == 149 and c1.avail == 1, c1.seats_raw
    assert c1.name == "民俗学" and c1.category == "通识课(通识核心课III)"
    assert pager["cur"] == 1 and pager["total"] == 2 and pager["has_next"]
    assert warned is False
    assert _PAGE["next_href"] == pager["next_href"]
    print("✓ 解析：14 列页数据 → 课程对象字段/限选/空余/状态 正确")


def test_filter():
    # 任一命中：分类命中即可
    fs = FilterSet(Filters(categories=["通识课(通识核心课III)"], depts=["英语语言文学系"],
                           match="any"))
    courses, _, _ = fetch._parse_page(_PAGE)
    hits = fs.matched(courses)
    assert [c.name for c in hits] == ["民俗学"], hits
    # 全部命中：民俗学不满足院系 → 不中
    fs_all = FilterSet(Filters(categories=["通识课(通识核心课III)"],
                               depts=["英语语言文学系"], match="all"))
    assert fs_all.matched(courses) == []
    # 空筛选 → 什么都不中
    assert FilterSet(Filters()).matched(courses) == []
    print("✓ 判断：任一/全部/空筛选 行为正确")


def _make_cfg(tmp: Path) -> Config:
    cfg = Config()
    cfg.credentials = cfg.credentials
    cfg.notify.to = "you@example.com"
    cfg.notify.gws_from = "sender@example.com"
    cfg.notify.min_interval_min = 15.0
    cfg.notify.max_per_hour = 5
    W.STATE_FILE = tmp / "notified.json"
    W.SEND_LOG_FILE = tmp / "send_log.json"
    cfg.filters = Filters(categories=["通识课(通识核心课III)"], depts=["英语语言文学系"],
                          match="any")
    return cfg


def test_notify_and_send():
    tmp = Path(tempfile.mkdtemp(prefix="courser-test-"))
    cfg = _make_cfg(tmp)
    w = W.Watcher(cfg, log=lambda m: None)

    courses, _, _ = fetch._parse_page(_PAGE)
    course = courses[1]  # 民俗学 avail=1

    # mock gws
    _calls.clear()
    notifier.shutil.which = lambda name: "/opt/homebrew/bin/gws" if name == "gws" else None
    notifier.subprocess.run = _fake_run

    sent = w._notify_seats([course])
    assert len(sent) == 1, sent
    # 同课冷却：立即再调 → 不再发
    assert w._notify_seats([course]) == []

    send_cmds = [c for c in _calls if "messages" in c and "send" in c]
    assert len(send_cmds) == 1, f"应恰好发送 1 次，实得 {len(send_cmds)}"
    cmd = send_cmds[0]
    assert cmd[0] == "gws" and cmd[2] == "users" and cmd[3] == "messages", cmd
    # --json 载荷 → raw(base64url MIME)
    payload = json.loads(cmd[cmd.index("--json") + 1])
    raw = payload["raw"]
    padded = raw + "=" * (-len(raw) % 4)
    mime_text = base64.urlsafe_b64decode(padded).decode("utf-8")
    from email import message_from_string
    from email.header import decode_header

    def _dh(v):
        return "".join(t.decode(charset or "utf-8") if isinstance(t, bytes) else t
                       for t, charset in decode_header(v))

    msg = message_from_string(mime_text)
    assert msg["From"] == "sender@example.com"
    assert msg["To"] == "you@example.com"
    assert _dh(msg["Subject"]) == "【选课提醒】补退选有空余名额：民俗学", msg["Subject"]
    html_part = ""
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            html_part = part.get_payload(decode=True).decode("utf-8")
            break
    assert html_part, "邮件中没有 text/html 部分"
    # 表格列与样式（含新增的「页」列）
    for col in ["页", "课程号", "课程名", "课程类别", "教师", "班号", "开课单位",
                "上课/考试信息", "限数/已选", "空余"]:
        assert col in html_part, f"缺少列 {col}"
    for banned in ["学分", "周学时", "年级", "选课状态", "自选P/NP", "状态"]:
        assert banned not in html_part, f"不应包含 {banned}"
    # 居中：页(th+td)、课程类别(th+td)、限数/已选(th+td) 共 6 处
    assert html_part.count('style="text-align:center;"') == 6, html_part
    assert html_part.count("<th>") + html_part.count('<th style="text-align:center;">') == 10
    print("✓ 发送：gws 命令、MIME、10 列表格（含页）、三类居中、无学分/年级/状态")


def test_email_order_and_page():
    """邮件需标注页码，且顺序与选课网一致（页号升序、同页从上到下——
    该顺序由 walk_pages 逐页追加保证，build_body 保持输入顺序）。"""
    from courser import notifier  # noqa: PLC0415
    c2 = Course(course_no="P2", name="A课(第2页)", category="通识课(通识核心课III)",
                dept="英语语言文学系", quota=50, selected=49, avail=1, page=2)
    c1 = Course(course_no="P1", name="B课(第1页)", category="通识课(通识核心课III)",
                dept="外国语学院", quota=50, selected=49, avail=1, page=1)
    text, html_body = notifier.build_body([c2, c1], "ts")
    # 输入顺序被保留（P2 在前），同时两种正文都带页码标注
    assert text.find("P2") < text.find("P1")
    assert "（第 2 页）" in text and "（第 1 页）" in text
    assert html_body.find("P2") < html_body.find("P1")
    # 页单元格内容正确
    assert ">2</td>" in html_body and ">1</td>" in html_body
    print("✓ 邮件：含页码标注；顺序保留 walk_pages 的选课网顺序")


def test_budget_and_cooldown():
    tmp = Path(tempfile.mkdtemp(prefix="courser-test-"))
    cfg = _make_cfg(tmp)
    w = W.Watcher(cfg, log=lambda m: None)
    courses, _, _ = fetch._parse_page(_PAGE)
    course = courses[1]

    _calls.clear()
    notifier.shutil.which = lambda name: "/opt/homebrew/bin/gws"
    notifier.subprocess.run = _fake_run

    # 预算：2 封/小时，日志里已有 2 条近期发送 → 跳过（查询不受影响）
    cfg.notify.max_per_hour = 2
    w._record_send()
    w._record_send()
    assert not w._budget_ok()
    sent = w._notify_seats([course])
    assert sent == [], "预算用尽应跳过发送"
    sends = [c for c in _calls if "messages" in c and "send" in c]
    assert sends == [], "预算用尽不应产生 gws send"

    # 1 小时后预算恢复
    W.SEND_LOG_FILE.write_text(json.dumps([1.0]), encoding="utf-8")  # 很久以前的记录
    assert w._budget_ok(), "过期记录不应占用预算"
    sent = w._notify_seats([course])
    assert len(sent) == 1
    print("✓ 预算：每小时上限生效，跳过发送但保住查询；过期记录自动释放")


def main() -> int:
    test_parse()
    test_filter()
    test_notify_and_send()
    test_email_order_and_page()
    test_budget_and_cooldown()
    print("=" * 60)
    print("全链路测试通过：解析 → 判断 → 冷却 → 预算 → 发送 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())