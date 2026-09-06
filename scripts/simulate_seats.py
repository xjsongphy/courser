"""模拟"有空余名额"触发邮件发送（不访问真实网页）。

构造符合当前筛选条件、且有空余名额的模拟课程，走与真实轮询完全相同的
「筛选匹配 → 通知文案 → gws 发送」链路，用于验证整条通知通道。

用法：
    uv run python scripts/simulate_seats.py

注意：需要 gws 已授权（gws auth login）；未授权时本脚本会在发送步骤给出明确提示。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from courser import notifier  # noqa: E402
from courser.config import Config, load_env_file  # noqa: E402
from courser.fetch import Course  # noqa: E402
from courser.filters import FilterSet  # noqa: E402


def main() -> int:
    load_env_file()
    cfg = Config.load()

    fake_courses = [
        Course(course_no="SIM0001", name="综合英语（模拟）", category="通识课(通识核心课I)",
               dept="英语语言文学系", quota=50, selected=49, avail=1,
               teacher="张三(教授)", class_no="1",
               schedule="1~16周 每周周二3~4节", status="可申请", seq="SIM-0001"),
        Course(course_no="SIM0002", name="模拟通选课（模拟）", category="通识课(通选课I)",
               dept="外国语学院", quota=40, selected=38, avail=2,
               teacher="李四(副教授)", class_no="1",
               schedule="1~16周 每周周四5~6节", status="可申请", seq="SIM-0002"),
    ]

    fs = FilterSet(cfg.filters)
    print(f"当前筛选：{fs.describe()}")
    print("模拟课程命中情况：")
    for c in fake_courses:
        hit = fs.matches(c)
        print(f"  {'✔ 命中' if hit else '✘ 未命中'} {c.name} | {c.category} | {c.dept}"
              f" | 限/选 {c.selected}/{c.quota} 空余 {c.avail}")

    matched = fs.matched(fake_courses)
    seats = [c for c in matched if c.has_seats]
    if not seats:
        print("没有模拟课程同时满足「命中筛选 + 有空余名额」，请检查 config.json 的 filters。")
        return 1

    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    subject = notifier.build_subject(seats)
    text, html_body = notifier.build_body(seats, ts)
    print("=" * 66)
    print(f"邮件主题：{subject}")
    print("-" * 66)
    print(text)
    print(f"（另附 HTML 表格正文，样式仿选课网，无「状态」列，共 {len(html_body)} 字符）")
    print("=" * 66)

    ok = notifier.send_email(cfg.notify, subject, text, body_html=html_body,
                             log=lambda m: print("[notify]", m))
    print("✔ 已触发邮件发送" if ok else "✗ 邮件发送未成功（原因见上方日志）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())