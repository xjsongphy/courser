"""发送一封测试邮件，验证 gws 邮件配置。

用法：
    uv run python scripts/test_mail.py
（收件邮箱取自 config.json / .env，或运行 courser 后在 TUI「设置 → 发送测试邮件」）
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from courser import notifier  # noqa: E402
from courser.config import Config, load_env_file  # noqa: E402


def main() -> int:
    load_env_file()
    cfg = Config.load()
    ok = notifier.send_email(cfg.notify, "【courser】测试邮件",
                             "这是 courser 发送的测试邮件。收到说明邮件通知配置正常。",
                             log=lambda m: print("[mail]", m))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())