"""整轮业务逻辑集成测试：抓取（注入 stub）→ 风控 → 筛选 → 通知决策 → 发送。

不连浏览器、不发真邮件（mock gws 子进程），用临时存储注入 RoundRunner。
用法：uv run python tests/integration/test_round_runner.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser import notifier  # noqa: E402
from courser.config import Config, Filters, Notify  # noqa: E402
from courser.models import Course, FetchResult, RoundResult  # noqa: E402
from courser.runner import RoundRunner  # noqa: E402
from courser.storage import (NotificationStateStore, SendBudgetStore,
                             SNAPSHOT_FILE)  # noqa: E402

SENT = []


def _fake_gws_run(cmd, **kwargs):
    if "messages" in cmd and "send" in cmd:
        SENT.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout='{"id":"m"}', stderr="")
    if "getProfile" in cmd:
        return types.SimpleNamespace(returncode=0,
                                     stdout='{"emailAddress":"prof@example.com"}', stderr="")
    return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")


def _enable_fake_gws():
    notifier.shutil.which = lambda n: "/usr/local/bin/gws" if n == "gws" else None
    notifier.subprocess.run = _fake_gws_run


def _mk_course(**kw) -> Course:
    base = dict(course_no="02030330", name="民俗学", category="通识课(通识核心课III)",
                dept="英语语言文学系", quota=150, selected=149, avail=1,
                status="可申请", seq="BZ202402030330_1")
    base.update(kw)
    return Course(**base)


def _make_runner(tmp: Path, max_per_hour: int = 5) -> RoundRunner:
    cfg = Config()
    cfg.notify.to = "you@example.com"
    cfg.notify.gws_from = "sender@example.com"
    cfg.notify.max_per_hour = max_per_hour
    cfg.filters = Filters(categories=["通识课(通识核心课III)"],
                          depts=["英语语言文学系"], match="any")
    notify_store = NotificationStateStore(tmp / "notified.json")
    budget_store = SendBudgetStore(tmp / "send_log.json")
    return RoundRunner(cfg, log=lambda m: None,
                       notify_store=notify_store, budget_store=budget_store)


def test_full_round_and_cooldown():
    _enable_fake_gws()
    SENT.clear()
    tmp = Path(tempfile.mkdtemp(prefix="rrunner-"))
    runner = _make_runner(tmp)

    def stub_fetch(**k):
        fr = FetchResult(login_mode="login_click", pages=2, ok=True)
        fr.courses = [_mk_course(avail=1),   # 命中+空余
                      _mk_course(name="近代物理实验", category="专业必修",
                                 dept="物理学院", quota=12, selected=12, avail=0)]
        return fr

    r = runner.run_round(fetch_round=stub_fetch)
    assert r.ok and r.total == 2 and r.pages == 2
    assert len(r.matched) == 1 and r.matched[0].name == "民俗学"
    assert len(r.notified) == 1 and SENT, "应触发一次 gws send"
    runner.last_result is r
    # 再次同轮（同一课程重复不应再发）
    SENT.clear()
    r2 = runner.run_round(fetch_round=stub_fetch)
    assert r2.notified == [] and SENT == [], "同课冷却：重复课程不应再发"
    print("✓ run：整轮状态/命中/发送/冷却 正确")


def test_warning_and_failure():
    _enable_fake_gws()
    SENT.clear()
    runner = _make_runner(Path(tempfile.mkdtemp(prefix="rrunner-")))

    def stub_warn(**k):
        fr = FetchResult(login_mode="login_click", pages=1, ok=True, warning_hit=True)
        fr.courses = []
        return fr

    r = runner.run_round(fetch_round=stub_warn)
    assert r.warning_hit and r.risk_percent == 100 and r.risk_label == "已触发/疑似"

    def stub_fail(**k):
        return FetchResult(login_mode="", pages=0, ok=False, error="登录失败(验证码)")

    r = runner.run_round(fetch_round=stub_fail)
    assert not r.ok and "登录失败" in r.error and r.notified == []
    # 回归：失败路径也必须收尾 duration + on_round + last_result（旧 bug）
    assert r.duration_s > 0, "失败路径也应记录耗时"
    assert runner.last_result is r, "失败路径也应更新 last_result"
    print("✓ run：风控置 100% 分支；登录失败分支不发送且正常收尾")


def test_retry_and_no_retry_on_warning():
    runner = _make_runner(Path(tempfile.mkdtemp(prefix="rrunner-")))
    runner.retry_delay_range = (0.1, 0.2)  # 测试用极短等待
    calls = {"n": 0}

    def flaky(**k):
        calls["n"] += 1
        if calls["n"] == 1:
            return FetchResult(login_mode="", pages=0, ok=False, error="网络抖了一下")
        fr = FetchResult(login_mode="login_click", pages=1, ok=True)
        fr.courses = [_mk_course(avail=1)]
        return fr

    r = runner.run_round(fetch_round=flaky)
    assert r.ok and calls["n"] == 2, f"应失败1次+重试1次，实际 {calls['n']} 次"
    assert len(r.courses) == 1
    print("✓ run：失败自动重试一次")

    calls["n"] = 0
    runner2 = _make_runner(Path(tempfile.mkdtemp(prefix="rrunner-")))
    runner2.retry_delay_range = (0.1, 0.2)

    def warn_then_fail(**k):
        calls["n"] += 1
        return FetchResult(login_mode="", pages=0, ok=False, error="x", warning_hit=True)

    r2 = runner2.run_round(fetch_round=warn_then_fail)
    assert calls["n"] == 1, "风控命中不应重试"
    print("✓ run：风控命中不重试")


def test_budget_and_snapshot():
    tmp = Path(tempfile.mkdtemp(prefix="rrunner-"))
    runner = _make_runner(tmp, max_per_hour=2)

    # 预算：2 封/小时，已有 2 条近期发送 → 拒绝
    runner.budget_store.record()
    runner.budget_store.record()
    assert not runner.budget_store.budget_ok(2)
    assert len(runner.budget_store.recent_sends()) == 2
    # 过期记录释放预算
    tmp2 = Path(tempfile.mkdtemp(prefix="rrunner-"))
    bs = SendBudgetStore(tmp2 / "send_log.json")
    (tmp2 / "send_log.json").write_text("""[1.0]""", encoding="utf-8")
    assert bs.budget_ok(5), "过期记录不应占用预算"
    print("✓ 预算：每小时上限生效；过期记录自动释放")

    # SnapshotStore：保存一轮 → 能还原课程/候选/元信息
    from courser.storage import SnapshotStore
    ss = SnapshotStore(tmp / "last_round.json")
    fr = FetchResult(pages=2, ok=True)
    fr.courses = [_mk_course(avail=1), _mk_course(name="X", category="专业必修")]
    r = RoundResult(pages=2, courses=fr.courses)
    r.total = 2
    distinct = ss.save(r, ts="2026-09-07 12:00:00")
    assert distinct["names"] == ["X", "民俗学"]  # sorted 后 ASCII 在前
    assert len(ss.load_courses()) == 2
    assert ss.candidates()["names"] == ["X", "民俗学"]
    ts, meta = ss.meta()
    assert ts == "2026-09-07 12:00:00" and "2 页 · 2 门课程" in meta
    print("✓ SnapshotStore：保存/还原课程、候选、元信息")


def test_on_progress_callback():
    """on_progress 应透传给一轮内的 fetch（--once 用它输出进度）。"""
    tmp = Path(tempfile.mkdtemp(prefix="rrunner-"))
    calls: list = []
    cfg = Config()
    cfg.notify.to = "x@y.z"
    runner = RoundRunner(cfg, log=lambda m: None,
                         on_progress=lambda d, t, o: calls.append((d, t, o)),
                         notify_store=NotificationStateStore(tmp / "n.json"),
                         budget_store=SendBudgetStore(tmp / "s.json"))

    def stub_fetch(**k):
        if k.get("on_progress"):
            k["on_progress"](1, None, "登出旧会话")
            k["on_progress"](3, 8, "正在读取课程列表 第 2/8 页")
        fr = FetchResult(pages=1, ok=True)
        fr.courses = []
        return fr

    runner.run_round(fetch_round=stub_fetch)
    assert len(calls) >= 2, f"进度回调应被调用，实际 {calls}"
    assert calls[0] == (1, None, "登出旧会话")
    assert calls[1] == (3, 8, "正在读取课程列表 第 2/8 页")
    print("✓ on_progress 透传给一轮内 fetch（--once 进度来源）")


def main() -> int:
    test_full_round_and_cooldown()
    test_warning_and_failure()
    test_retry_and_no_retry_on_warning()
    test_budget_and_snapshot()
    test_on_progress_callback()
    print("=" * 60)
    print("round runner 集成测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
