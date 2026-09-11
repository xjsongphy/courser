"""config 单元测试：load/save 往返、非法上限兜底、文件缺失默认、.env 优先级。

用法：uv run python tests/unit/test_config.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.config import Config  # noqa: E402


def test_load_save_roundtrip():
    tmp = Path(tempfile.mkdtemp(prefix="courser-cfg-"))
    p = tmp / "config.json"
    cfg = Config()
    cfg.interval_min = 7.5
    cfg.notify.max_per_hour = 3
    cfg.credentials.username = "stu"
    cfg.filters.categories = ["通识课(通选课III)"]
    cfg.save(p)
    again = Config.load(p)
    assert again.interval_min == 7.5
    assert again.notify.max_per_hour == 3
    assert again.credentials.username == "stu"
    assert again.filters.categories == ["通识课(通选课III)"]
    print("✓ config：load/save 往返")


def test_bounds_and_missing():
    tmp = Path(tempfile.mkdtemp(prefix="courser-cfg-"))
    p = tmp / "config.json"
    # 兜底：max_per_hour 非法→>=1
    p.write_text(json.dumps({"notify": {"max_per_hour": -3}}), encoding="utf-8")
    assert Config.load(p).notify.max_per_hour >= 1
    # 不存在的文件 → 默认值
    assert Config.load(tmp / "missing.json").interval_min == 8.0
    print("✓ config：非法上限兜底、文件缺失默认")


def test_env_priority():
    os.environ["PKU_USERNAME"] = "env_user"
    from courser.config import Credentials
    c = Credentials.from_dict(None)
    assert c.username == "env_user", ".env / 环境变量应填充凭据"
    # 显式 dict 优先于 env
    c2 = Credentials.from_dict({"username": "dict_user"})
    assert c2.username == "dict_user"
    del os.environ["PKU_USERNAME"]
    print("✓ config：显式配置优先于环境变量")


def test_reload_picks_up_external_edit():
    """运行中外部改 config.json 后，reload() 应就地更新同一对象（config 热刷新）。"""
    tmp = Path(tempfile.mkdtemp(prefix="courser-reload-"))
    p = tmp / "config.json"
    cfg = Config()
    cfg.credentials.username = "old_user"
    cfg.interval_min = 99.0
    cfg.save(p)
    cfg.reload(p)
    assert cfg.credentials.username == "old_user"

    # 模拟外部进程直接改 config.json（这是 TUI 运行时最常见的情况）
    p.write_text(json.dumps({"credentials": {"username": "new_user"},
                             "interval_min": 3.5}), encoding="utf-8")
    same_ref = cfg  # 记录同一对象引用，验证就地在原对象上更新
    cfg.reload(p)
    assert cfg is same_ref, "reload 不应替换对象引用"
    assert cfg.credentials.username == "new_user", "应读到外部新值"
    assert cfg.interval_min == 3.5
    print("✓ config：运行中 reload() 感知外部改动（热刷新）")


def main() -> int:
    test_load_save_roundtrip()
    test_bounds_and_missing()
    test_env_priority()
    test_reload_picks_up_external_edit()
    print("=" * 60)
    print("config 单元测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
