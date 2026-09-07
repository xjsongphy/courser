"""单元测试补充：覆盖 test_pipeline 尚未触及的模块。

- config：load/save 往返、.env 优先级、max_per_hour 兜底
- filters：describe / active_groups
- risk：BotRisk 档位、页码警告文本、has_warning_text
- notifier：build_body 列齐、send_email 的 gws 缺失/to 为空 失败分支、_gws_profile_email
- watcher.run_round：注入 stub fetch_round，验证 整轮 状态/命中/发送调用、
  风控提示分支、登录失败分支

不连浏览器、不发真邮件（mock gws 子进程）。
用法：uv run python scripts/test_unit.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 使用临时配置与临时日志，避免测试污染真实 config.json / data/courser.log
_tmpenv = Path(tempfile.mkdtemp(prefix="courser-tunit-"))
os.environ["COURSER_CONFIG"] = str(_tmpenv / "config.json")
os.environ["COURSER_LOG"] = str(_tmpenv / "courser.log")

import courser.watcher as W  # noqa: E402
from courser import fetch, filters, notifier, risk  # noqa: E402
from courser.config import Config, Filters, Notify  # noqa: E402
from courser.fetch import Course  # noqa: E402

SENT = []


def _fake_gws_run(cmd, **kwargs):
    if "messages" in cmd and "send" in cmd:
        SENT.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout='{"id":"m"}', stderr="")
    if "getProfile" in cmd:
        return types.SimpleNamespace(returncode=0, stdout='{"emailAddress":"prof@example.com"}', stderr="")
    return types.SimpleNamespace(returncode=0, stdout="{}", stderr="")


def _enable_fake_gws():
    notifier.shutil.which = lambda n: "/usr/local/bin/gws" if n == "gws" else None
    notifier.subprocess.run = _fake_gws_run


def _mk_course(**kw) -> Course:
    base = dict(course_no="02030330", name="民俗学", category="通识课(通识核心课III)",
                credits="2.0", weekly_hours="2.0", teacher="王娟(教授)", class_no="1",
                dept="英语语言文学系", grade="2024", schedule="1~16周 每周周二5~6节",
                quota=150, selected=149, avail=1, status="可申请", seq="BZ202402030330_1")
    base.update(kw)
    return Course(**base)


def _tmp_cfg(max_per_hour: int = 5) -> tuple[Config, Path]:
    tmp = Path(tempfile.mkdtemp(prefix="courser-unit-"))
    W.STATE_FILE = tmp / "notified.json"
    W.SEND_LOG_FILE = tmp / "send_log.json"
    os.environ["COURSER_CONFIG"] = str(tmp / "config.json")
    cfg = Config()
    cfg.notify.to = "you@example.com"
    cfg.notify.gws_from = "sender@example.com"
    cfg.notify.max_per_hour = max_per_hour
    cfg.filters = Filters(categories=["通识课(通识核心课III)"], depts=["英语语言文学系"],
                          match="any")
    return cfg, tmp


# ---------- opencli ----------
def test_opencli_eval_coercion():
    """回归：opencli eval 的布尔/数字是裸字符串，必须还原成 Python 类型，
    否则 _form_present/_on_workable_page 的 `is True` 恒 False → 登录永远失败。"""
    from courser.opencli import _parse_eval_output  # noqa: PLC0415
    assert _parse_eval_output("true") is True
    assert _parse_eval_output("false") is False
    assert _parse_eval_output("null") is None
    assert _parse_eval_output("42") == 42
    assert _parse_eval_output('{"a":1}') == {"a": 1}
    assert _parse_eval_output('[1,2]') == [1, 2]
    assert _parse_eval_output("账号登录\n扫码登录") == "账号登录\n扫码登录"
    print("✓ opencli：eval 输出 布尔/数字/JSON/文本 还原正确（登录在位判断依赖此）")


# ---------- config ----------
def test_config_roundtrip():
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
    # 兜底：max_per_hour 非法→>=1
    p.write_text(json.dumps({"notify": {"max_per_hour": -3}}), encoding="utf-8")
    assert Config.load(p).notify.max_per_hour >= 1
    # 不存在的文件 → 默认值
    assert Config.load(tmp / "missing.json").interval_min == 8.0
    print("✓ config：load/save 往返、非法上限兜底、文件缺失默认")


# ---------- filters ----------
def test_filters_desc():
    f = Filters(names=["攀岩"], categories=["通识课(通选课III)"], match="any")
    fs = filters.FilterSet(f)
    c = _mk_course(name="攀岩", category="全校必修", dept="体育教研部")
    assert fs.matches(c), "任一命中：课程名命中即可"
    assert "任一命中" in fs.describe() or "任一" in fs.describe()
    f2 = Filters(names=["攀岩"], categories=["通识课(选)"], match="all")
    assert not filters.FilterSet(f2).matches(_mk_course(category="全校必修")), \
        "全部命中：类别不满足则应不中"
    print("✓ filters：describe / active 组合匹配")


# ---------- risk ----------
def test_risk():
    r = risk.BotRisk()
    assert not risk.has_warning_text("正常内容")
    assert risk.has_warning_text("请勿使用刷课机，否则限制选课")
    assert risk.has_warning_text("访问过于频繁")
    lo, _ = r.evaluate(pages=4, duration_s=240)   # <5 rpm → 低
    assert lo < 20, lo
    hi, _ = r.evaluate(pages=8, duration_s=20)
    assert hi >= 75, hi
    r2 = risk.BotRisk(); r2.mark_warning()
    s, lab = r2.evaluate(pages=2, duration_s=60)
    assert s == 100 and lab == "已触发/疑似"
    print("✓ risk：频率档位、风控文案检测、命中即 100%")


# ---------- notifier ----------
def test_notifier_branches():
    # to 为空
    n = Notify(to="")
    assert not notifier.send_email(n, "s", "b")
    # gws 缺失
    real = notifier.shutil.which
    notifier.shutil.which = lambda _: None
    assert not notifier.send_email(Notify(to="x@y.z"), "s", "b")
    notifier.shutil.which = real
    # 成功 + From 兜底用 gws profile
    _enable_fake_gws(); SENT.clear()
    ok = notifier.send_email(Notify(to="you@example.com"), "主题", "正文")
    assert ok
    assert notifier._gws_profile_email() == "prof@example.com"
    print("✓ notifier：to为空 / gws缺失 失败分支；profile 邮箱兜底")


# ---------- watcher.run_round（注入 stub fetch） ----------
def test_run_round_full():
    cfg, tmp = _tmp_cfg()
    w = W.Watcher(cfg, log=lambda m: None)
    _enable_fake_gws(); SENT.clear()

    def stub_fetch(**k):
        fr = fetch.FetchResult(login_mode="login_click", pages=2, ok=True)
        fr.courses = [_mk_course(avail=1),   # 命中+空余
                      _mk_course(name="近代物理实验", category="专业必修", dept="物理学院",
                                 quota=12, selected=12, avail=0)]  # 不命中
        return fr

    r = w.run_round(fetch_round=stub_fetch)
    assert r.ok
    assert r.total == 2 and r.pages == 2
    assert len(r.matched) == 1 and r.matched[0].name == "民俗学"
    assert len(r.notified) == 1
    assert SENT, "应触发一次 gws send"
    # 再次同轮（同一课程重复不应再发）
    SENT.clear()
    r2 = w.run_round(fetch_round=stub_fetch)
    assert r2.notified == [] and SENT == [], "同课冷却：重复课程不应再发"
    print("✓ run_round：整轮状态/命中/发送/冷却 正确")


def test_run_round_warning_and_fail():
    cfg, tmp = _tmp_cfg()
    w = W.Watcher(cfg, log=lambda m: None)
    _enable_fake_gws(); SENT.clear()

    def stub_warn(**k):
        fr = fetch.FetchResult(login_mode="login_click", pages=1, ok=True, warning_hit=True)
        fr.courses = []
        return fr

    r = w.run_round(fetch_round=stub_warn)
    assert r.warning_hit and r.risk_percent == 100 and r.risk_label == "已触发/疑似"

    def stub_fail(**k):
        return fetch.FetchResult(login_mode="", pages=0, ok=False, error="登录失败(验证码)")

    r = w.run_round(fetch_round=stub_fail)
    assert not r.ok and "登录失败" in r.error and r.notified == []
    print("✓ run_round：风控置 100% 分支；登录失败分支不发送")


def test_run_round_retry():
    """失败重试：整轮失败且非风控时，重试一次后成功。"""
    cfg, tmp = _tmp_cfg()
    w = W.Watcher(cfg, log=lambda m: None)
    w.retry_delay_range = (0.1, 0.2)  # 测试用极短等待
    calls = {"n": 0}

    def flaky(**k):
        calls["n"] += 1
        if calls["n"] == 1:
            return fetch.FetchResult(login_mode="", pages=0, ok=False, error="网络抖了一下")
        fr = fetch.FetchResult(login_mode="login_click", pages=1, ok=True)
        fr.courses = [_mk_course(avail=1)]
        return fr

    r = w.run_round(fetch_round=flaky)
    assert r.ok and calls["n"] == 2, f"应失败1次+重试1次，实际调用 {calls['n']} 次"
    assert len(r.courses) == 1
    # 风控命中时绝不重试
    calls["n"] = 0
    w2 = W.Watcher(_tmp_cfg()[0], log=lambda m: None)
    w2.retry_delay_range = (0.1, 0.2)

    def warn_then_fail(**k):
        calls["n"] += 1
        return fetch.FetchResult(login_mode="", pages=0, ok=False, error="x", warning_hit=True)

    r = w2.run_round(fetch_round=warn_then_fail)
    assert calls["n"] == 1, "风控命中不应重试"
    print("✓ run_round：失败自动重试一次；风控命中不重试")


def main() -> int:
    test_opencli_eval_coercion()
    test_config_roundtrip()
    test_filters_desc()
    test_risk()
    test_notifier_branches()
    test_run_round_full()
    test_run_round_warning_and_fail()
    test_run_round_retry()
    print("=" * 60)
    print("单元测试全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())