"""翻页状态机重试回归测试：重复页（点 Next 后仍在原页）不再立即终止。

状态机的意义就是重试：点 Next 后内容签名没变（翻页未生效）时连续重试
_PAGER_RETRIES 次，页面真的变了才接受；重试用尽仍不变才结束本轮（少抓几页，
由下一轮从第 1 页继续）。硬停只留给风控警告/登录失效/未知页。

用法：uv run python tests/integration/test_pager_retry.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ["COURSER_CONFIG"] = str(Path(tempfile.mkdtemp()) / "config.json")

from courser.pku import client  # noqa: E402
from courser.pku.extract import EXTRACT_JS, PAGE_STATE_JS  # noqa: E402

HEADER = ["课程号", "课程名", "课程类别", "学分", "周学时", "教师", "班号",
          "开课单位", "年级", "上课时间", "P/NP", "限数", "选课状态", "备注"]


def _page_data(no: str, name: str, *, has_next: bool,
               cur: int = 1, total: int = 4) -> dict:
    return {
        "has_next": has_next,
        "next_href": "http://x/next",
        "pager": {"cur": cur, "total": total},
        "tables": [{
            "header": HEADER,
            "rows": [{"cells": [no, name, "任选", "2", "2", "张老师", "B1",
                                "物理学院", "2026", "周一", "", "40 / 10",
                                "可申请", ""],
                      "links": [{"t": "补选", "h": "http://x/goNested.do?course_seq_no=" + no}]}],
            "pager_here": False,
        }],
    }


def _fetch(extract_handler, log=None):
    """替换 oc 的 eval_js/click_by + 跳过抓取前准备，直接跑 walk_pages。"""
    extract_calls = {"n": 0}

    def eval_js(session, js: str):
        if js == PAGE_STATE_JS:
            return {"ready": True, "warning": False}
        if js == EXTRACT_JS:
            extract_calls["n"] += 1
            return extract_handler(extract_calls["n"])
        raise AssertionError(f"unexpected eval_js: {js[:50]!r}")

    def click_by(*_a, **_k):
        return True

    saved = (client.oc.eval_js, client.oc.click_by,
             client.prepare_fetch_context, client.sleep_rand)
    client.oc.eval_js = eval_js
    client.oc.click_by = click_by
    client.prepare_fetch_context = lambda session, **k: "reuse_session"
    client.sleep_rand = lambda *a, **k: 0.0
    try:
        return client.fetch_round(session="s", pacing=(0.01, 0.02), log=log)
    finally:
        (client.oc.eval_js, client.oc.click_by,
         client.prepare_fetch_context, client.sleep_rand) = saved


def test_repeat_page_recovers_by_retry():
    """P1 → 连续两次点击后仍在 P1（重复页）→ 第 3 次重试翻到 P2：
    必须完整抓到 P1+P2，且不重复 P1（不绕过去重）。"""
    logs: list[str] = []

    def extract(call):
        if call <= 3:          # 初始 P1 + 两次重复页重读
            return _page_data("0001", "课程1", has_next=True, cur=1, total=4)
        return _page_data("0002", "课程2", has_next=False, cur=2, total=4)

    fr = _fetch(extract, log=logs.append)
    assert fr.ok, "重试后翻页成功，本轮应正常完成"
    assert fr.pages == 2, f"应抓到 2 页，实际 {fr.pages}"
    seqs = [c.seq for c in fr.courses]
    assert seqs == ["0001", "0002"], f"应按页序完整抓取且无重复，实际 {seqs}"
    assert any("仍在原页" in m for m in logs), f"应有重试日志，实际 {logs[:3]}"
    assert not any("提前结束" in m for m in logs), f"重试成功就不应提前结束，{logs}"
    print("✓ 重复页重试恢复：P1→(同页×2)→P2，抓全且不重复")


def test_repeat_page_gives_up_after_page_window():
    """单页窗口：同一页连续 _PAGER_RETRIES 次重试仍不变 → 本轮判失败
    （不产生重复课程、不死循环；由整轮重建重试覆盖）。"""
    logs: list[str] = []

    def extract(_call):        # 永远同页
        return _page_data("0001", "课程1", has_next=True, cur=1, total=4)

    fr = _fetch(extract, log=logs.append)
    assert not fr.ok, "单页重试达上限应判本轮失败"
    assert "翻页未生效" in fr.error, fr.error
    assert fr.pages == 1, f"异常路径也应收回已读页数，实际 {fr.pages}"
    assert fr.courses == [], "本轮判失败，部分页课程不带走（不发通知）"
    print(f"✓ 单页窗口：连续 {client._PAGER_RETRIES} 次重试仍不变 → 本轮失败，无重复、不循环")


def test_round_retry_budget_window():
    """整轮窗口：没有任何一页单独触顶，但累计翻页重试达到整轮预算 → 本轮判失败。
    模拟：每页容许 5 次重试（单页窗口很宽），但整轮预算只有 2 次，
    第 2 次翻页重试就应触发预算上限（即使单页窗口远未触顶）。"""
    saved = (client._PAGER_RETRIES, client._ROUND_PAGER_RETRIES)
    client._PAGER_RETRIES = 5
    client._ROUND_PAGER_RETRIES = 2
    try:
        logs: list[str] = []

        def extract(_call):        # 永远同页
            return _page_data("0001", "课程1", has_next=True, cur=1, total=4)

        fr = _fetch(extract, log=logs.append)
    finally:
        client._PAGER_RETRIES, client._ROUND_PAGER_RETRIES = saved

    assert not fr.ok, "整轮预算触顶应判本轮失败"
    assert "本轮累计重试已达上限" in fr.error, fr.error
    assert fr.pages == 1
    print("✓ 整轮窗口：单页未触顶但累计达预算 → 本轮判失败（预算语义生效）")


def main() -> int:
    test_repeat_page_recovers_by_retry()
    test_repeat_page_gives_up_after_page_window()
    test_round_retry_budget_window()
    print("=" * 60)
    print(f"翻页双层窗口测试通过 ✅（单页 {client._PAGER_RETRIES} 次 · "
          f"整轮预算 {client._ROUND_PAGER_RETRIES} 次）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())