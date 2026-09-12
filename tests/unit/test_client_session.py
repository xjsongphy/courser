"""登录会话复用单元测试。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.pku import client  # noqa: E402


def _page(kind, *, page=None):
    return client.PageObservation(kind=kind, page=page)


def test_reuses_valid_session_without_login():
    original_detect, original_login = client.detect_page, client.login
    try:
        client.detect_page = lambda _session: _page(client.PageKind.ELECTIVE_HOME)
        client.login = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("有效会话不应进入登录流程"))
        logs = []
        mode = client.ensure_login("test", force_relogin=False, log=logs.append)
    finally:
        client.detect_page, client.login = original_detect, original_login

    assert mode == "reuse_session"
    assert any("复用当前会话" in message for message in logs)


def test_session_expired_has_priority_over_supplement_url():
    """超时提示必须压过仍残留的 supplement URL 和菜单。"""
    original_eval = client.oc.eval_js
    try:
        client.oc.eval_js = lambda *_args, **_kwargs: {
            "url": "https://elective.pku.edu.cn/elective2008/.../supplement.jsp",
            "has_elective_menu": True,
            "has_course_table": False,
            "session_expired": True,
            "risk_warning": False,
            "has_login_form": False,
            "has_captcha": False,
        }
        observed = client.detect_page("test")
    finally:
        client.oc.eval_js = original_eval

    assert observed.kind == client.PageKind.SESSION_EXPIRED


def test_invalid_session_falls_back_to_login():
    original_detect, original_login = client.detect_page, client.login
    calls = []
    try:
        client.detect_page = lambda _session: _page(client.PageKind.UNKNOWN)
        client.login = lambda *_args, **kwargs: calls.append(kwargs) or "login_click"
        mode = client.ensure_login("test", force_relogin=False)
    finally:
        client.detect_page, client.login = original_detect, original_login

    assert mode == "login_click"
    assert calls == [{"creds": None, "window": None, "force_logout": False,
                      "log": None, "prog": None}]


def test_expired_session_exits_before_relogin():
    """过期页保留 supplement URL 时，也必须退出旧会话后再登录。"""
    original_detect, original_login = client.detect_page, client.login
    calls, logs = [], []
    try:
        client.detect_page = lambda _session: _page(client.PageKind.SESSION_EXPIRED)
        client.login = lambda *_args, **kwargs: calls.append(kwargs) or "login_click"
        mode = client.ensure_login("test", force_relogin=False, log=logs.append)
    finally:
        client.detect_page, client.login = original_detect, original_login

    assert mode == "login_click"
    assert calls[0]["force_logout"] is True
    assert any("会话已过期" in message for message in logs)


def test_force_relogin_skips_session_probe():
    original_detect, original_login = client.detect_page, client.login
    calls = []
    try:
        client.detect_page = lambda _session: (_ for _ in ()).throw(
            AssertionError("强制重登不应探测会话"))
        client.login = lambda *_args, **kwargs: calls.append(kwargs) or "login_click"
        mode = client.ensure_login("test", force_relogin=True)
    finally:
        client.detect_page, client.login = original_detect, original_login

    assert mode == "login_click"
    assert calls == [{"creds": None, "window": None, "force_logout": True,
                      "log": None, "prog": None}]


def test_fetch_round_routes_through_ensure_login():
    original_prepare = client.prepare_fetch_context
    original_walk = client.walk_pages
    calls = []
    try:
        client.prepare_fetch_context = lambda *args, **kwargs: calls.append(kwargs) or "reuse_session"
        client.walk_pages = lambda *_args, **_kwargs: ([], {"pages": 1, "warning_hit": False})
        result = client.fetch_round("test", force_logout=False)
    finally:
        client.prepare_fetch_context = original_prepare
        client.walk_pages = original_walk

    assert result.ok and result.login_mode == "reuse_session"
    assert calls == [{"creds": None, "window": None, "force_relogin": False,
                      "log": None, "prog": None}]


def test_prepare_fetch_context_runs_workflow_steps_in_order():
    original_ensure = client.ensure_login
    original_goto = client.goto_supplement
    original_reset = client.reset_supplement_to_first_page
    calls = []
    try:
        client.ensure_login = lambda *_args, **_kwargs: calls.append("login") or "reuse_session"
        client.goto_supplement = lambda *_args, **_kwargs: calls.append("supplement")
        client.reset_supplement_to_first_page = lambda *_args, **_kwargs: calls.append("first")
        mode = client.prepare_fetch_context("test", force_relogin=False)
    finally:
        client.ensure_login = original_ensure
        client.goto_supplement = original_goto
        client.reset_supplement_to_first_page = original_reset

    assert mode == "reuse_session"
    assert calls == ["login", "supplement", "first"]


def test_prepare_fetch_context_logs_ready_state():
    original_ensure = client.ensure_login
    original_goto = client.goto_supplement
    original_reset = client.reset_supplement_to_first_page
    original_page = client._supplement_page_number
    logs = []
    try:
        client.ensure_login = lambda *_args, **_kwargs: "reuse_session"
        client.goto_supplement = lambda *_args, **_kwargs: "https://example.test/SupplyCancel.do"
        client.reset_supplement_to_first_page = lambda *_args, **_kwargs: None
        client._supplement_page_number = lambda *_args, **_kwargs: 1
        client.prepare_fetch_context("test", log=logs.append)
    finally:
        client.ensure_login = original_ensure
        client.goto_supplement = original_goto
        client.reset_supplement_to_first_page = original_reset
        client._supplement_page_number = original_page

    assert logs == [
        "抓取准备：登录状态=复用已有会话",
        "抓取准备：补退选页面=https://example.test/SupplyCancel.do",
        "抓取准备：当前页=1",
    ]


def test_reused_supplement_page_is_reset_before_walking():
    original_detect = client.detect_page
    original_click = client.oc.click_by
    original_sleep = client.sleep_rand
    try:
        pages = iter([_page(client.PageKind.SUPPLEMENT, page=8),
                      _page(client.PageKind.SUPPLEMENT, page=1)])
        client.detect_page = lambda *_args, **_kwargs: next(pages)
        clicks = []
        client.oc.click_by = lambda *_args, **kwargs: clicks.append(kwargs) or True
        client.sleep_rand = lambda *_args: None
        logs = []
        client.reset_supplement_to_first_page("test", log=logs.append)
    finally:
        client.detect_page = original_detect
        client.oc.click_by = original_click
        client.sleep_rand = original_sleep

    assert clicks == [{"role": "link", "name": "First"}]
    assert any("第 8 页" in message for message in logs)
    assert any("第 1 页" in message for message in logs)


def test_menu_without_table_interactive_is_transitioning():
    """点击 Next 后常出现 menu 已渲染、table 未完、readyState=interactive。
    必须归 TRANSITIONING，而不是凭菜单误判成 ELECTIVE_HOME，否则
    walk_pages 会在一次正常但稍慢的翻页上过早失败。"""
    saved = client.oc.eval_js
    try:
        client.oc.eval_js = lambda _session, _js: {
            "url": "https://elective.pku.edu.cn/elective2008/...",
            "ready_state": "interactive",
            "has_login_form": False,
            "has_elective_menu": True,
            "has_course_table": False,
            "session_expired": False,
            "risk_warning": False,
            "has_captcha": False,
            "page": None,
            "total_pages": None,
        }
        obs = client.detect_page("s")
        assert obs.kind == client.PageKind.TRANSITIONING, obs.kind
    finally:
        client.oc.eval_js = saved

    # 对照组：同样的 menu、但 table 已就绪 => 仍是 SUPPLEMENT（课程表优先）
    try:
        client.oc.eval_js = lambda _session, _js: {
            "url": "https://elective.pku.edu.cn/...",
            "ready_state": "complete",
            "has_login_form": False,
            "has_elective_menu": True,
            "has_course_table": True,
            "session_expired": False,
            "risk_warning": False,
            "has_captcha": False,
            "page": 1,
            "total_pages": 8,
        }
        obs = client.detect_page("s")
        assert obs.kind == client.PageKind.SUPPLEMENT, obs.kind
    finally:
        client.oc.eval_js = saved
    print("✓ menu=True+table=False+interactive -> TRANSITIONING（而非 ELECTIVE_HOME）；有表仍 SUPPLEMENT")


def test_goto_supplement_captcha_is_not_risk_blocked():
    """点击补退选后出现验证码必须保留 CAPTCHA 语义，不能误记为风控。"""
    saved = (client.detect_page, client.oc.click, client.sleep_rand)
    states = iter([
        _page(client.PageKind.ELECTIVE_HOME),
        _page(client.PageKind.CAPTCHA),
    ])
    try:
        client.detect_page = lambda *_args, **_kwargs: next(states)
        client.oc.click = lambda *_args, **_kwargs: {"clicked": True, "matches_n": 1}
        client.sleep_rand = lambda *_args, **_kwargs: None
        try:
            client.goto_supplement("s")
            raise AssertionError("验证码页应抛 CaptchaError")
        except client.CaptchaError:
            pass
    finally:
        client.detect_page, client.oc.click, client.sleep_rand = saved
    print("✓ 点击补退选后验证码：CaptchaError（不误分类为 RiskBlockedError）")


def main() -> int:
    test_reuses_valid_session_without_login()
    test_session_expired_has_priority_over_supplement_url()
    test_invalid_session_falls_back_to_login()
    test_expired_session_exits_before_relogin()
    test_force_relogin_skips_session_probe()
    test_fetch_round_routes_through_ensure_login()
    test_prepare_fetch_context_runs_workflow_steps_in_order()
    test_prepare_fetch_context_logs_ready_state()
    test_reused_supplement_page_is_reset_before_walking()
    test_menu_without_table_interactive_is_transitioning()
    test_goto_supplement_captcha_is_not_risk_blocked()
    print("登录会话复用单元测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
