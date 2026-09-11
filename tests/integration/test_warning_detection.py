"""风控警告「采集层」回归测试：真实浏览器 workflow 三态判定。

之前只 stub 了 FetchResult(warning_hit=True,...)，绕过了真正有 bug 的链路：
    浏览器页面 → _wait_table → EXTRACT_JS → walk_pages → FetchResult.warning_hit
这里 mock client.oc 的 eval_js/click_by，直接跑 client.fetch_round，覆盖：

- 页面无课程表 + 出现「请勿使用刷课机」→ warning_hit=True, warning_checked=True
  （此前会等到超时 → 被误判成「0 页正常完成」→ 漏报甚至反向记 false）
- 前 1~3 页正常、第 4 页风控阻断 → warning_hit=True（而非机会性漏掉）
- 读取中异常也要保留已观察页数 → 不把「查过几页」当「0 页无观察」

用法：uv run python tests/integration/test_warning_detection.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.pku import client  # noqa: E402
from courser.pku.extract import EXTRACT_JS, PAGE_STATE_JS  # noqa: E402


# -- 真实 EXTRACT_JS 输出结构的最小页面数据 ------------------------------
HEADER = ["课程号", "课程名", "课程类别", "学分", "周学时", "教师", "班号",
          "开课单位", "年级", "上课时间", "P/NP", "限数", "选课状态", "备注"]


def _page_data(no: str, name: str, *, has_next: bool,
               cur: int = 1, total: int = 4) -> dict:
    return {
        "has_next": has_next,
        "next_href": "http://x/next",
        "pager": {"cur": cur, "total": total},
        "tables": [{
            "header": HEADER,
            "rows": [{"cells": [no, name, "任选", "2", "2", "张老师", "B1",
                                "物理学院", "2026", "周一", "", "40 / 10",
                                "可申请", ""],
                      "links": [{"t": "补选",
                                 "h": "...electSupplement.do?...course_seq_no=" + no}]}],
            "pager_here": False,
        }],
    }


class _OCHarness:
    """记录现场并代替 client.oc：eval_js 按注入的 handler 分发。"""

    def __init__(self, page_state, extract):
        self.page_state = page_state       # fn(session)->dict 或 dict
        self.extract = extract             # fn(session, call_index)->dict | raise
        self.extract_calls = 0
        self.clicked = 0

    def eval_js(self, session, js: str):
        if js == PAGE_STATE_JS:
            return self.page_state(session) if callable(self.page_state) else self.page_state
        if js == EXTRACT_JS:
            self.extract_calls += 1
            return self.extract(session, self.extract_calls)
        raise AssertionError(f"unexpected eval_js: {js[:50]!r}")

    def click_by(self, *args, **kw):
        self.clicked += 1
        return True


def _fetch(harness):
    """替换 oc 的 eval_js/click_by + 跳过抓取前准备，直接测抓取链路。
    注：远端已把登录/进入补退选/重置页码集中到 prepare_fetch_context，
    这里只 mock 它返回登录方式，让 walk_pages 走真正浏览器三态判定。"""
    saved = (client.oc.eval_js, client.oc.click_by,
             client.prepare_fetch_context, client.sleep_rand)
    client.oc.eval_js = harness.eval_js
    client.oc.click_by = harness.click_by
    client.prepare_fetch_context = lambda session, **k: "reuse_session"
    client.sleep_rand = lambda *a, **k: 0.0
    try:
        return client.fetch_round(session="s", pacing=(0.01, 0.02))
    finally:
        (client.oc.eval_js, client.oc.click_by,
         client.prepare_fetch_context, client.sleep_rand) = saved


def test_warning_page_only():
    """页面无课程表 + 命中警告文案 → warning_hit=True, warning_checked=True。"""
    harness = _OCHarness(
        page_state={"ready": False, "warning": True},
        extract=lambda s, i: (_ for _ in ()).throw(AssertionError("警告页不应执行 EXTRACT_JS")),
    )
    fr = _fetch(harness)
    assert fr.warning_hit is True, "必须判定为命中风控警告"
    assert fr.warning_checked is True, "警告页即使 pages=0 也是有观察的有效样本"
    assert fr.pages == 0
    # 结论：这次 attempt 应计入风控样本为 True（hit），而不是漏报/反向成 False
    print("✓ 纯警告页：warning_hit=True, warning_checked=True, pages=0")


def test_expired_session_page_is_not_reported_as_empty_courses():
    """会话超时页应立即失败，让下轮重登；不能等 25 秒后报 0 门课。"""
    harness = _OCHarness(
        page_state={"ready": False, "warning": False, "session_expired": True},
        extract=lambda s, i: (_ for _ in ()).throw(AssertionError("超时页不应执行 EXTRACT_JS")),
    )
    fr = _fetch(harness)
    assert not fr.ok
    assert "会话超时" in fr.error
    assert fr.pages == 0 and fr.warning_checked is False
    print("✓ 会话超时页：立即识别为登录失效，不误报 0 门课程")


def test_warning_after_normal_pages():
    """前 3 页正常、第 4 页风控阻断 → warning_hit=True 而非机会性漏掉。"""
    state_calls = {"n": 0}

    def page_state(s):
        state_calls["n"] += 1
        # 每次翻页前也会 observe 一次：P1 前/P1 后/P2 前/P2 后/P3 前/P3 后/P4。
        if state_calls["n"] >= 7:           # 第 4 页触发风控、课程表消失
            return {"ready": False, "warning": True}
        return {"ready": True, "warning": False}

    def extract(s, i):
        if i <= 3:
            return _page_data(f"0200000{i}", f"课程{i}", has_next=(i < 4),
                              cur=i, total=4)
        return _page_data("0200999", "超页", has_next=False, cur=4, total=4)

    harness = _OCHarness(page_state=page_state, extract=extract)
    fr = _fetch(harness)
    assert fr.pages == 3, f"应抓到前 3 页（第 4 页风控阻断），实际 {fr.pages}"
    assert fr.warning_hit is True, "触发了风控就应记录为命中，而不是 false"
    assert fr.warning_checked is True
    print("✓ 前 3 页正常 + 第 4 页风控：pages=3, warning_hit=True")


def test_exception_keeps_observed_pages():
    """读取中途异常也要带回已观察页数 → warning_checked=True（P1）。"""
    def page_state(s):
        return {"ready": True, "warning": False}

    def extract(s, i):
        if i == 1:
            return _page_data("0200001", "课程1", has_next=True, cur=1, total=2)
        raise client.oc.OpenCliError(["eval", "x"], "{}", "翻页后提取异常", 1)

    harness = _OCHarness(page_state=page_state, extract=extract)
    fr = _fetch(harness)
    assert not fr.ok and "翻页后提取异常" in fr.error
    assert fr.pages == 1, "异常应保留已读到的 1 页，而非归 0"
    assert fr.warning_checked is True, "已读过 ≥1 页，即使异常也是有观察的有效样本"
    print("✓ 读取中异常：保留 pages=1, warning_checked=True（不丢观察）")


def test_captcha_during_fetch_is_typed_captcha():
    """抓取中验证码：走结构化的 failure_kind=CAPTCHA，不靠中文文本解析。"""
    from courser.models import FetchFailureKind
    harness = _OCHarness(
        page_state={"has_captcha": True, "ready": False, "warning": False},
        extract=lambda s, i: (_ for _ in ()).throw(AssertionError("验证码页不应执行 EXTRACT_JS")),
    )
    fr = _fetch(harness)
    assert not fr.ok, "验证码应判定失败"
    assert fr.failure_kind == FetchFailureKind.CAPTCHA, fr.failure_kind
    print("✓ 抓取中验证码：failure_kind=CAPTCHA（结构化，不靠字符串）")


def test_session_expired_during_fetch_is_typed_auth_expired():
    """抓取中会话超时：走结构化的 failure_kind=AUTH_EXPIRED。"""
    from courser.models import FetchFailureKind
    harness = _OCHarness(
        page_state={"session_expired": True, "ready": False, "warning": False},
        extract=lambda s, i: (_ for _ in ()).throw(AssertionError("超时页不应执行 EXTRACT_JS")),
    )
    fr = _fetch(harness)
    assert not fr.ok, "会话超时应判定失败"
    assert fr.failure_kind == FetchFailureKind.AUTH_EXPIRED, fr.failure_kind
    print("✓ 抓取中会话超时：failure_kind=AUTH_EXPIRED（结构化）")


def test_risk_blocked_before_fetch_counts_as_hit():
    """进入 fetch 前(ensure_login/detect_page)就被风控阻断：failure_kind=RISK_BLOCKED。
    必须记成 warning_hit=True——否则会同时打出"本轮失败：当前处于风控阻断页"却
    "本次：未触发刷课机警告"的矛盾日志，并漏记一次真实风控样本。"""
    from courser.models import FetchFailureKind, RiskBlockedError
    saved = (client.prepare_fetch_context, client.oc.eval_js,
             client.oc.click_by, client.sleep_rand)

    def raise_risk(*_a, **_k):
        raise RiskBlockedError("当前处于风控阻断页，停止本轮")

    client.prepare_fetch_context = raise_risk
    client.oc.eval_js = lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("不应再走到页面读取"))
    client.oc.click_by = lambda *a, **k: True
    client.sleep_rand = lambda *a, **k: 0.0
    try:
        fr = client.fetch_round(session="s")
    finally:
        (client.prepare_fetch_context, client.oc.eval_js,
         client.oc.click_by, client.sleep_rand) = saved

    assert not fr.ok
    assert fr.failure_kind == FetchFailureKind.RISK_BLOCKED, fr.failure_kind
    assert fr.warning_hit is True and fr.warning_checked is True, \
        (fr.warning_hit, fr.warning_checked)
    print("✓ 进入 fetch 前风控阻断：warning_hit=True（计入命中，不再误报“未触发”）")


def main() -> int:
    test_warning_page_only()
    test_expired_session_page_is_not_reported_as_empty_courses()
    test_warning_after_normal_pages()
    test_exception_keeps_observed_pages()
    test_captcha_during_fetch_is_typed_captcha()
    test_session_expired_during_fetch_is_typed_auth_expired()
    test_risk_blocked_before_fetch_counts_as_hit()
    print("=" * 60)
    print("风控警告采集层测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
