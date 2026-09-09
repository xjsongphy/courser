"""opencli 适配器单元测试：eval 输出的类型还原。

回归：opencli eval 的布尔/数字是裸字符串，必须还原成 Python 类型，
否则 _form_present / _on_workable_page 的 `is True` 恒 False → 登录永远失败。
用法：uv run python tests/unit/test_opencli.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser import opencli  # noqa: E402
from courser.opencli import _parse_eval_output  # noqa: E402


def test_eval_coercion():
    assert _parse_eval_output("true") is True
    assert _parse_eval_output("false") is False
    assert _parse_eval_output("null") is None
    assert _parse_eval_output("42") == 42
    assert _parse_eval_output('{"a":1}') == {"a": 1}
    assert _parse_eval_output('[1,2]') == [1, 2]
    assert _parse_eval_output("账号登录\n扫码登录") == "账号登录\n扫码登录"
    print("✓ opencli：eval 输出 布尔/数字/JSON/文本 还原正确")


def test_cancel_terminates_inflight_command():
    """退出请求不应等到 opencli 的 120 秒命令超时。"""
    event = threading.Event()

    class BlockedProcess:
        returncode = None

        def __init__(self):
            self.terminated = False

        def communicate(self, timeout=None):
            if not self.terminated:
                event.set()
                raise subprocess.TimeoutExpired("opencli", timeout)
            return "", ""

        def terminate(self):
            self.terminated = True

        def kill(self):
            self.terminated = True

    proc = BlockedProcess()
    original = opencli.subprocess.Popen
    opencli.subprocess.Popen = lambda *args, **kwargs: proc
    try:
        with opencli.cancellation_scope(event):
            try:
                opencli._run("test", ["state"], timeout=120)
            except opencli.OpenCliCancelled:
                pass
            else:
                raise AssertionError("取消中的命令必须抛出 OpenCliCancelled")
    finally:
        opencli.subprocess.Popen = original
    assert proc.terminated


def main() -> int:
    test_eval_coercion()
    test_cancel_terminates_inflight_command()
    print("=" * 60)
    print("opencli 单元测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
