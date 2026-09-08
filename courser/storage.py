"""统一持久化存储。

集中管理所有 JSON 落地文件（snapshot / 通知状态 / 发送预算），替代此前散落在
TUI / watcher 里各写各的 `read_text` / `write_text` + `json.loads` / `json.dumps`。

- 只接受**最新**格式：旧版本字段或结构不符一律当作空状态，从不做迁移，也绝不因
  一次损坏的文件把主流程带崩（IO 出错 → 返回当前内存态 / 空态）。
- 每个 store 都可在构造时传入路径，便于测试用临时目录，不污染真实 data/。

路径默认收敛在 data/ 下（gitignored）。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from .config import DATA_DIR
from .models import Course, RoundResult, is_real_course

SNAPSHOT_FILE = DATA_DIR / "last_round.json"
STATE_FILE = DATA_DIR / "notified.json"
SEND_LOG_FILE = DATA_DIR / "send_log.json"


def _atomic_write(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                    encoding="utf-8")


def _read_json(path: Path) -> Optional[dict]:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        pass
    return None


class SnapshotStore:
    """最近一轮抓取结果的快照（含候选维度列表），供筛选页/主页展示。

    格式（最新）：
        {"ts": str, "pages": int, "n": int, "distinct": {names,categories,depts},
         "courses": [Course.__dict__, ...]}
    """

    def __init__(self, path: Path = SNAPSHOT_FILE):
        self.path = Path(path)

    def load(self) -> Optional[dict]:
        """返回原始快照 dict；不存在/损坏 → None。"""
        return _read_json(self.path)

    def load_courses(self) -> list[Course]:
        """从快照还原课程列表（供主页/筛选候选），剔除被误存的表格脚/分页栏。"""
        d = self.load()
        if not d:
            return []
        try:
            courses = [Course(**{k: v for k, v in c.items()})
                       for c in d.get("courses", [])]
            return [c for c in courses if is_real_course(c)]
        except Exception:
            return []

    def candidates(self) -> dict[str, list[str]]:
        """从快照还原候选维度列表（用于筛选页）。"""
        d = self.load()
        if not d:
            return {"names": [], "categories": [], "depts": []}
        du = d.get("distinct") or {}
        return {k: list(v) for k, v in du.items()}

    def meta(self) -> tuple[Optional[str], str]:
        """返回 (ts, meta 文本)；无快照 → (None, '')。"""
        d = self.load()
        if not d:
            return None, ""
        ts = d.get("ts")
        pages, n = d.get("pages"), d.get("n")
        meta = f"{pages} 页 · {n} 门课程" if pages is not None else ""
        return ts, meta

    def risk(self) -> Optional[tuple]:
        """返回 (risk_percent, risk_label)；旧快照缺失风控信息 → None（未知）。"""
        d = self.load()
        if not d:
            return None
        p, lab = d.get("risk_percent"), d.get("risk_label")
        if p is None:
            return None
        return int(p), str(lab or "无")

    def save(self, r: RoundResult, ts: Optional[str] = None) -> dict:
        """保存一轮结果并返回 distinct 候选。"""
        distinct = {
            "names": sorted({c.name for c in r.courses if c.name}),
            "categories": sorted({cat for c in r.courses if c.category for cat in c.categories}),
            "depts": sorted({c.dept for c in r.courses if c.dept}),
        }
        ts = ts or time.strftime("%Y-%m-%d %H:%M:%S")
        d = {"ts": ts, "pages": r.pages, "n": len(r.courses),
             "risk_percent": r.risk_percent, "risk_label": r.risk_label,
             "distinct": distinct, "courses": [c.__dict__ for c in r.courses]}
        try:
            _atomic_write(self.path, d)
        except Exception:
            pass
        return distinct


class NotificationStateStore:
    """课程通知去重状态：course key -> {avail, quota, selected, ts, name, seq, dept, category}。"""

    def __init__(self, path: Path = STATE_FILE):
        self.path = Path(path)
        self.state: dict = _read_json(self.path) or {}

    def get(self, key: str) -> dict:
        return self.state.get(key, {})

    def mark(self, c: Course) -> None:
        self.state[c.key] = {
            "avail": c.avail, "quota": c.quota, "selected": c.selected,
            "ts": time.time(), "name": c.name, "seq": c.seq,
            "dept": c.dept, "category": c.category,
        }

    def save(self) -> None:
        try:
            _atomic_write(self.path, self.state)
        except Exception:
            pass


class SendBudgetStore:
    """每小时发送上限：只记录发送时间戳，读取/写入都会按 1 小时窗口裁剪。"""

    def __init__(self, path: Path = SEND_LOG_FILE):
        self.path = Path(path)

    def _timestamps(self) -> list[float]:
        try:
            if self.path.exists():
                return [float(t) for t in json.loads(self.path.read_text(encoding="utf-8"))]
        except Exception:
            pass
        return []

    def recent_sends(self) -> list[float]:
        now = time.time()
        return [t for t in self._timestamps() if now - t < 3600]

    def budget_ok(self, max_per_hour: int) -> bool:
        return len(self.recent_sends()) < max_per_hour

    def record(self) -> None:
        recent = self.recent_sends()
        recent.append(time.time())
        try:
            _atomic_write(self.path, recent)
        except Exception:
            pass
