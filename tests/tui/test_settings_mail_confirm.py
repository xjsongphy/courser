"""设置页「发送一封测试邮件」后台异步发送回归测试（无头）。

覆盖：
- 邮件分组下新增动作行出现；设置页仍显示底部全局活动状态栏
- 首次回车进入确认窗口：动作行转为「再次回车确认发送」，底部保持正常操作提示
- 第二次回车：**立即**进入「发送中…」，且事件循环不被阻塞（↑↓ 仍可移动光标）
- 后台线程发送完成后：显示「发送成功/失败」；2 秒后自动恢复「回车发送」
- 超时（无第二次回车）：自动恢复原提示、不发送

用法：uv run python tests/tui/test_settings_mail_confirm.py
（不启动监控、不连网络；发送走注入的假 gws）
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 使用临时配置，避免冒烟测试污染真实 config.json
_tmpdir = tempfile.mkdtemp(prefix="courser-tmail-")
os.environ["COURSER_CONFIG"] = str(Path(_tmpdir) / "config.json")

import courser.notifier as _notifier  # noqa: E402

from courser.config import Config  # noqa: E402
from courser.tui.app import CourserApp  # noqa: E402


async def _wait_until(p, cond, timeout: float = 4.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return
        await p.pause(0.05)
    raise AssertionError("等待后台发送完成超时")


def _mail_row_index(app) -> int:
    return next(i for i, (_g, f) in enumerate(app.s_rows)
                if f["key"] == "test_mail")


def _settings_text(app) -> str:
    return str(app.query_one("#settings_list").render())


def _keys_text(app) -> str:
    return str(app.query_one("#keys").render())


async def _open_settings(pilot) -> None:
    await pilot.press("s")
    await pilot.pause(0.3)
    # assert app.page == "settings"


def _install_fake_gws(app) -> dict:
    """注入带可控时延的假 gws 发送：记录调用、阻塞片刻，返回成功。
    这样能稳定验证「发送中…」窗口与后台线程不阻塞 UI。"""
    app.sd["to"] = "test@example.com"
    state = {"calls": 0, "gate": 1.5}

    def fake_gws_available() -> bool:
        return True

    def fake_send(*args, **kwargs):
        state["calls"] += 1
        time.sleep(state["gate"])          # 模拟 gws 网络耗时，给 UI 展示「发送中」
        log = kwargs.get("log")
        if log:
            log("测试邮件已发送")
        return True

    app._fake_mail_restore = (
        _notifier.gws_available, _notifier.send_email_with_retry)
    _notifier.gws_available = fake_gws_available
    _notifier.send_email_with_retry = fake_send
    return state


async def main() -> int:
    # ---- 确认发送路径（后台异步，不阻塞 UI）----
    cfg = Config.load()
    cfg.first_run_done = True
    app = CourserApp(cfg)
    # 注入超短超时：确认窗口 & 结果清除从默认 2s 压到 0.2s，测试不必真等 2 秒
    app.TEST_MAIL_CONFIRM_TIMEOUT_S = 0.2
    app.TEST_MAIL_RESULT_CLEAR_S = 0.2
    try:
        async with app.run_test(size=(120, 36)) as p:
            await p.pause(0.3)
            await _open_settings(p)
            assert app.page == "settings", "s 应进入设置页"
            assert app.query_one("#activity").display, "设置页应显示底部全局活动状态"
            idx = _mail_row_index(app)
            _g, f = app.s_rows[idx]
            assert f["kind"] == "action" and f["label"] == "发送一封测试邮件", f
            for _ in range(idx):
                await p.press("down")
            await p.pause(0.2)
            assert app.s_rows[app.sv.index][1]["key"] == "test_mail", \
                "光标应停在测试邮件行"

            # 首次回车 → 确认窗口：动作行转为「再次回车确认发送」
            await p.press("enter")
            await p.pause(0.05)   # 确认窗口注入 0.2s：短间隔断言，不与超时竞态
            assert app.sv.test_mail_armed_at is not None, "首次回车应进入确认窗口"
            body = _settings_text(app)
            assert "再次回车确认发送" in body, "动作行应提示再次回车确认发送"
            assert "回车发送" not in body, "确认后不应再显示普通「回车发送」"
            assert "↑↓ 选择" in _keys_text(app), "底部操作提示应在固定 #keys 栏"

            fake = _install_fake_gws(app)

            # 第二次回车 → 立即进入发送中（后台线程），不阻塞 UI
            await p.press("enter")
            await p.pause(0.05)
            assert app.sv.test_mail_armed_at is None, "确认后应退出确认窗口"
            assert app.sv.test_mail_sending is True, "第二次回车应立即进入发送中"
            assert "发送中" in _settings_text(app), "UI 应即时显示「发送中…」"

            # 事件循环不被阻塞：发送期间 ↑↓ 仍可移动光标（后台线程发送）
            i0 = app.sv.index
            await p.press("down")
            await p.pause(0.05)
            assert app.sv.index == min(i0 + 1, len(app.s_rows) - 1), \
                "发送中 ↑↓ 仍应可移动光标（不抢光标/不冻结）"
            await p.press("up")
            await p.pause(0.05)
            assert app.sv.index == i0
            assert "发送中" in _settings_text(app), "发送期间列表仍正常渲染"

            # 等待后台线程完成 → 发送成功
            await _wait_until(p, lambda: not app.sv.test_mail_sending)
            assert fake["calls"] == 1, "应恰好发送一次"
            assert app.sv.test_mail_result is True
            assert "发送成功" in _settings_text(app)
            joined = "\n".join(app.log_buf)
            assert "已发送" in joined, joined

            # 2 秒后结果自动清除 → 恢复「回车发送」（注入后为 0.2s）
            await _wait_until(p, lambda: app.sv.test_mail_result is None)
            assert app.sv.test_mail_result is None, "结果应自动清除"
            assert "回车发送" in _settings_text(app), "应恢复普通「回车发送」"
            assert "发送成功" not in _settings_text(app)
    finally:
        # 还原注入的假 gws（只输入时可能未触发 _install_fake_gws）
        if hasattr(app, "_fake_mail_restore"):
            _notifier.gws_available, _notifier.send_email_with_retry = \
                app._fake_mail_restore

    # ---- 超时取消路径 ----
    cfg2 = Config.load()
    cfg2.first_run_done = True
    app2 = CourserApp(cfg2)
    app2.TEST_MAIL_CONFIRM_TIMEOUT_S = 0.2   # 注入超短超时，压掉真实 2 秒等待
    async with app2.run_test(size=(120, 36)) as p2:
        await p2.pause(0.3)
        await _open_settings(p2)
        idx = _mail_row_index(app2)
        for _ in range(idx):
            await p2.press("down")
        await p2.pause(0.2)
        await p2.press("enter")
        await p2.pause(0.05)   # 确认窗口注入 0.2s：短间隔断言，不与超时竞态
        assert app2.sv.test_mail_armed_at is not None
        assert "再次回车确认发送" in _settings_text(app2)
        # 不再按键，等确认窗口超时自动取消（注入后为 0.2s）
        await _wait_until(p2, lambda: app2.sv.test_mail_armed_at is None,
                          timeout=2.0)
        assert app2.sv.test_mail_armed_at is None, "未确认应自动取消"
        body = _settings_text(app2)
        assert "再次回车确认发送" not in body, "超时后应恢复正常提示"
        assert "↑↓ 选择" in _keys_text(app2), "超时后应恢复普通操作提示"
        joined = "\n".join(app2.log_buf)
        assert "超时取消" in joined, joined

    print("设置页测试邮件：后台异步发送 · 发送中/成功/自动恢复 · 不阻塞UI不抢光标/超时取消 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))