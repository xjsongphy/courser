"""风控风险评估单元测试。

用法：uv run python tests/unit/test_risk.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.risk import BotRisk, has_warning_text  # noqa: E402


def test_risk():
    r = BotRisk()
    assert not has_warning_text("正常内容")
    assert has_warning_text("请勿使用刷课机，否则限制选课")
    assert has_warning_text("访问过于频繁")
    lo, _ = r.evaluate(pages=4, duration_s=240)   # <5 rpm → 低
    assert lo < 20, lo
    hi, _ = r.evaluate(pages=8, duration_s=20)
    assert hi >= 75, hi
    r2 = BotRisk()
    r2.mark_warning()
    s, lab = r2.evaluate(pages=2, duration_s=60)
    assert s == 100 and lab == "已触发/疑似"
    print("✓ risk：频率档位、风控文案检测、命中即 100%")


def main() -> int:
    test_risk()
    print("=" * 60)
    print("risk 单元测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
