"""登录会话复用单元测试。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.pku import client  # noqa: E402


def test_reuses_valid_session_without_login():
    original_probe, original_login = client._on_workable_page, client.login
    try:
        client._on_workable_page = lambda _session: True
        client.login = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("有效会话不应进入登录流程"))
        logs = []
        mode = client.ensure_login("test", force_relogin=False, log=logs.append)
    finally:
        client._on_workable_page, client.login = original_probe, original_login

    assert mode == "reuse_session"
    assert any("复用当前会话" in message for message in logs)


def test_invalid_session_falls_back_to_login():
    original_probe, original_login = client._on_workable_page, client.login
    calls = []
    try:
        client._on_workable_page = lambda _session: False
        client.login = lambda *_args, **kwargs: calls.append(kwargs) or "login_click"
        mode = client.ensure_login("test", force_relogin=False)
    finally:
        client._on_workable_page, client.login = original_probe, original_login

    assert mode == "login_click"
    assert calls == [{"creds": None, "window": None, "force_logout": False,
                      "log": None, "prog": None}]


def test_force_relogin_skips_session_probe():
    original_probe, original_login = client._on_workable_page, client.login
    calls = []
    try:
        client._on_workable_page = lambda _session: (_ for _ in ()).throw(
            AssertionError("强制重登不应探测会话"))
        client.login = lambda *_args, **kwargs: calls.append(kwargs) or "login_click"
        mode = client.ensure_login("test", force_relogin=True)
    finally:
        client._on_workable_page, client.login = original_probe, original_login

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
    original_eval = client.oc.eval_js
    original_click = client.oc.click_by
    original_sleep = client.sleep_rand
    try:
        pages = iter([8, 1])
        client.oc.eval_js = lambda *_args, **_kwargs: next(pages)
        clicks = []
        client.oc.click_by = lambda *_args, **kwargs: clicks.append(kwargs) or True
        client.sleep_rand = lambda *_args: None
        logs = []
        client.reset_supplement_to_first_page("test", log=logs.append)
    finally:
        client.oc.eval_js = original_eval
        client.oc.click_by = original_click
        client.sleep_rand = original_sleep

    assert clicks == [{"role": "link", "name": "First"}]
    assert any("第 8 页" in message for message in logs)
    assert any("第 1 页" in message for message in logs)


def main() -> int:
    test_reuses_valid_session_without_login()
    test_invalid_session_falls_back_to_login()
    test_force_relogin_skips_session_probe()
    test_fetch_round_routes_through_ensure_login()
    test_prepare_fetch_context_runs_workflow_steps_in_order()
    test_prepare_fetch_context_logs_ready_state()
    test_reused_supplement_page_is_reset_before_walking()
    print("登录会话复用单元测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
