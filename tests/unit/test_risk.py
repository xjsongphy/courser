"""风控风险评估单元测试：实际警告事件的 rolling rate（persisted history）。

用法：uv run python tests/unit/test_risk.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.risk import RiskHistory, has_warning_text, risk_label  # noqa: E402


def test_risk():
    # 文案检测
    assert not has_warning_text("正常内容")
    assert has_warning_text("请勿使用刷课机，否则限制选课")
    assert has_warning_text("访问过于频繁")

    # 等级分档
    assert risk_label(0) == "无"
    assert risk_label(10) == "低"
    assert risk_label(30) == "中"
    assert risk_label(60) == "高"
    assert risk_label(100) == "极高"

    # rolling rate：10 次里 2 次触发 → 20%
    h = RiskHistory()
    assert h.evaluate() == (None, 0, 0), "无样本应判未知"
    for _ in range(8):
        h.record(False)
    h.record(True)
    h.record(True)
    assert h.evaluate() == (20, 2, 10)

    # 只保留 maxlen（20 条）：超出丢弃最早样本
    h2 = RiskHistory()
    for i in range(25):
        h2.record(i % 5 == 0)   # 0,5,10,15,20 为 True
    assert len(h2.samples) == 20, "应只保留最近 20 条"
    assert h2.evaluate() == (20, 2, 10), "最近 10 条里 15、20 两次触发"

    # 持久化：record 后 reload 状态保持
    tmp = Path(tempfile.mkdtemp(prefix="risk-"))
    hp = RiskHistory(path=tmp / "risk.json")
    hp.record(True)
    hp2 = RiskHistory(path=tmp / "risk.json")
    assert hp2.evaluate() == (100, 1, 1), "重载后应读到已记录样本"
    print("✓ risk：文案检测、等级分档、rolling rate、maxlen、持久化")


def main() -> int:
    test_risk()
    print("=" * 60)
    print("risk 单元测试通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())