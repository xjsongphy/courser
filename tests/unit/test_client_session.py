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
    original_ensure = client.ensure_login
    original_goto = client.goto_supplement
    original_walk = client.walk_pages
    calls = []
    try:
        client.ensure_login = lambda *args, **kwargs: calls.append(kwargs) or "reuse_session"
        client.goto_supplement = lambda *_args, **_kwargs: "https://elective.pku.edu.cn/SupplyCancel.do"
        client.walk_pages = lambda *_args, **_kwargs: ([], {"pages": 1, "warning_hit": False})
        result = client.fetch_round("test", force_logout=False)
    finally:
        client.ensure_login = original_ensure
        client.goto_supplement = original_goto
        client.walk_pages = original_walk

    assert result.ok and result.login_mode == "reuse_session"
    assert calls == [{"creds": None, "window": None, "force_relogin": False,
                      "log": None, "prog": None}]


def main() -> int:
    test_reuses_valid_session_without_login()
    test_invalid_session_falls_back_to_login()
    test_force_relogin_skips_session_probe()
    test_fetch_round_routes_through_ensure_login()
    print("登录会话复用单元测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
