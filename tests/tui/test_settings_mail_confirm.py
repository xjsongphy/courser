"""设置页「发送一封测试邮件」二次确认回归测试（无头）。

覆盖：
- 邮件分组下新增动作行出现；设置页仍显示底部全局活动状态栏
- 首次回车进入确认窗口：动作行转为「再次回车确认发送」，底部保持正常操作提示
- 2 秒内第二次回车：真正发送（走 _test_mail_draft，未配置邮箱时落日志、不落盘）
- 超时（无第二次回车）：自动恢复原提示、不发送

用法：uv run python tests/tui/test_settings_mail_confirm.py
（不启动监控、不连网络）
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# 使用临时配置，避免冒烟测试污染真实 config.json
_tmpdir = tempfile.mkdtemp(prefix="courser-tmail-")
os.environ["COURSER_CONFIG"] = str(Path(_tmpdir) / "config.json")

from courser.config import Config  # noqa: E402
from courser.tui.app import CourserApp  # noqa: E402


def _mail_row_index(app) -> int:
    return next(i for i, (_g, f) in enumerate(app.s_rows)
                if f["key"] == "test_mail")


def _settings_text(app) -> str:
    return str(app.query_one("#settings_list").render())


async def _open_settings(pilot) -> None:
    await pilot.press("s")
    await pilot.pause(0.3)
    # assert app.page == "settings"


async def main() -> int:
    # ---- 确认发送路径 ----
    cfg = Config.load()
    cfg.first_run_done = True
    app = CourserApp(cfg)
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

        # 首次回车 → 确认窗口：动作行转为「再次回车确认发送」，底部保持正常操作提示
        await p.press("enter")
        await p.pause(0.2)
        assert app.sv.test_mail_armed_at is not None, "首次回车应进入确认窗口"
        body = _settings_text(app)
        assert "再次回车确认发送" in body, "动作行应提示再次回车确认发送"
        assert "回车发送" not in body, "确认后不应再显示普通「回车发送」"
        assert "↑↓ 选择" in body, "底部操作提示应保持不变"
        # 顶部只留 gws 状态，不再有 t / Ctrl+S
        assert "t 测试邮件" not in body and "Ctrl+S" not in body.split("\n")[1], body

        # 2 秒内第二次回车 → 发送（无收件邮箱 → 日志报错，不崩溃）
        await p.press("enter")
        await p.pause(0.3)
        assert app.sv.test_mail_armed_at is None, "确认后应退出确认窗口"
        body = _settings_text(app)
        assert "再次回车确认发送" not in body, "发送后应恢复正常提示"
        assert "回车发送" in body, "发送后动作行应恢复普通「回车发送」"
        joined = "\n".join(app.log_buf)
        assert ("收件邮箱为空" in joined or "gws 未安装" in joined
                or "已发送" in joined), joined

    # ---- 超时取消路径 ----
    cfg2 = Config.load()
    cfg2.first_run_done = True
    app2 = CourserApp(cfg2)
    async with app2.run_test(size=(120, 36)) as p2:
        await p2.pause(0.3)
        await _open_settings(p2)
        idx = _mail_row_index(app2)
        for _ in range(idx):
            await p2.press("down")
        await p2.pause(0.2)
        await p2.press("enter")
        await p2.pause(0.2)
        assert app2.sv.test_mail_armed_at is not None
        assert "再次回车确认发送" in _settings_text(app2)
        # 不再按键，等 2 秒超时
        await p2.pause(2.4)
        assert app2.sv.test_mail_armed_at is None, "2s 未确认应自动取消"
        body = _settings_text(app2)
        assert "再次回车确认发送" not in body, "超时后应恢复正常提示"
        assert "↑↓ 选择" in body, "超时后应恢复普通操作提示"
        joined = "\n".join(app2.log_buf)
        assert "超时取消" in joined, joined

    print("设置页测试邮件二次确认 OK：确认发送 / 超时取消 均正常 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))