"""_failure_kind 结构化分类回归测试：BROWSER_UNAVAILABLE 与 BROWSER_ERROR 分离。

- 浏览器可用后单条操作失败（OpenCliError, browser_ready=True） → BROWSER_ERROR（重试一次）
- 登录前即失败 / 命令整体超时（Chrome 桥根本没起来） → BROWSER_UNAVAILABLE（不重试、停监控）
- 验证码/会话超时/登录失败/风控 → 原有映射不变

用法：uv run python tests/unit/test_failure_kind.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.models import (AuthExpiredError, CaptchaError, FetchFailureKind,  # noqa: E402
                            LoginError, RiskBlockedError)
from courser.pku import client  # noqa: E402


def test_browser_unavailable_vs_error_split():
    err = client.oc.OpenCliError(["browser", "s", "eval"], "out", "桥不可达", 1)
    timed = subprocess.TimeoutExpired(["browser", "s"], 120)

    # 登录前就失败（browser_ready=False）→ BROWSER_UNAVAILABLE
    assert client._failure_kind(err, browser_ready=False) == \
        FetchFailureKind.BROWSER_UNAVAILABLE, "连不上浏览器应判不可用"
    # 命令整体超时（连 Chrome 等 60~120s）→ BROWSER_UNAVAILABLE
    assert client._failure_kind(timed, browser_ready=False) == \
        FetchFailureKind.BROWSER_UNAVAILABLE
    assert client._failure_kind(timed, browser_ready=True) == \
        FetchFailureKind.BROWSER_UNAVAILABLE, "超时不分阶段都是桥不可用"

    # 浏览器已可用、某条操作失败 → BROWSER_ERROR（可重试一次）
    assert client._failure_kind(err, browser_ready=True) == \
        FetchFailureKind.BROWSER_ERROR, "浏览器可用后的单条失败应是 BROWSER_ERROR"
    print("✓ BROWSER_UNAVAILABLE(登录前失败/超时) 与 BROWSER_ERROR(浏览器可用后) 分离")


def test_existing_mappings_unchanged():
    assert client._failure_kind(CaptchaError("验证码")) == FetchFailureKind.CAPTCHA
    assert client._failure_kind(AuthExpiredError("会话超时")) == FetchFailureKind.AUTH_EXPIRED
    assert client._failure_kind(RiskBlockedError("风控")) == FetchFailureKind.RISK_BLOCKED
    assert client._failure_kind(LoginError("账号被拒")) == FetchFailureKind.AUTH_FAILED
    assert client._failure_kind(RuntimeError("未知")) == FetchFailureKind.PAGE_UNKNOWN
    print("✓ 原有映射不变")


def main() -> int:
    test_browser_unavailable_vs_error_split()
    test_existing_mappings_unchanged()
    print("=" * 60)
    print("failure_kind 分类测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())