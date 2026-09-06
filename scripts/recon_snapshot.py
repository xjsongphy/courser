"""一次性侦察：完整执行一轮「登录 → 补退选 → 全页抓取」，
把可用课程列表保存到 data/courses_snapshot.json，
并汇总去重后的 课程名 / 课程类别 / 开课院系 候选值（TUI 筛选界面使用）。

用法：
    uv run python scripts/recon_snapshot.py [--session pku] [--window foreground|background]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from courser import fetch  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="抓取补退选可用课程列表快照")
    ap.add_argument("--session", default="pku-recon", help="opencli 会话名")
    ap.add_argument("--window", default="foreground", choices=["foreground", "background"])
    ap.add_argument("--out", default="data/courses_snapshot.json")
    ap.add_argument("--pacing", default="6,14", help="翻页随机间隔范围(秒)")
    args = ap.parse_args()

    lo, hi = (float(x) for x in args.pacing.split(","))
    log = lambda m: print(f"[recon] {m}", flush=True)

    log(f"会话={args.session} window={args.window} 开始抓取…")
    r = fetch.fetch_round(session=args.session, creds=None, window=args.window,
                          pacing=(lo, hi), force_logout=True, log=log)

    if not r.ok:
        print(f"[recon] ✗ 抓取失败：{r.error}", file=sys.stderr)
        return 1

    courses = r.courses
    distinct = {
        "names": sorted({c.name for c in courses if c.name}),
        "categories": sorted({c.category for c in courses if c.category}),
        "depts": sorted({c.dept for c in courses if c.dept}),
    }
    has_seats = [c for c in courses if c.has_seats]
    snapshot = {
        "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
        "login_mode": r.login_mode,
        "pages": r.pages,
        "n": len(courses),
        "distinct": distinct,
        "courses": [
            {k: v for k, v in c.__dict__.items() if k != "links"}
            | {"links": {k: (v[:1] if isinstance(v, list) else v)
                         for k, v in c.links.items()}}
            for c in courses
        ],
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    log(f"✔ 完成：{r.pages} 页，{len(courses)} 门课程，其中 {len(has_seats)} 门有空余名额")
    log(f"  去重候选：课程名 {len(distinct['names'])} 个 / "
        f"课程类别 {len(distinct['categories'])} 个 / "
        f"开课院系 {len(distinct['depts'])} 个")
    log(f"  快照已保存：{out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())