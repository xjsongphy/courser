"""配置管理。

- config.json：TUI 可编辑的全部配置（轮询间隔、筛选条件、凭据、邮件、浏览器会话）
- 环境变量（.env）：敏感信息（密码等）可选覆盖
所有配置集中在「设置」界面维护，代码里不散落魔法配置。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = Path(os.environ.get("COURSER_CONFIG", str(PROJECT_ROOT / "config.json")))
DATA_DIR = PROJECT_ROOT / "data"
STATE_FILE = DATA_DIR / "notified.json"
SEND_LOG_FILE = DATA_DIR / "send_log.json"   # 每小时发送预算的发送时间戳日志


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default)


@dataclass
class Credentials:
    username: str = ""   # 学号；留空则依赖密码管理器自动填充
    password: str = ""   # 密码；留空则依赖密码管理器自动填充

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Credentials":
        d = d or {}
        return cls(
            username=str(d.get("username", "") or _env("PKU_USERNAME", "")),
            password=str(d.get("password", "") or _env("PKU_PASSWORD", "")),
        )


@dataclass
class Notify:
    """邮件通知（通过 gws = Google Workspace CLI 发送，需用户自行安装并 gws auth login）。"""

    to: str = ""                       # 收件人邮箱（提醒的目标地址，TUI 设置 / MAIL_TO 提供）
    gws_from: str = ""                 # gws 发件账号（Gmail 地址，可选；默认取认证账号）
    min_interval_min: float = 15.0     # 同一课程两次通知的最小间隔（分钟）
    max_per_hour: int = 5              # 每小时最多发送的邮件封数（只限发信，不影响查询轮次）

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Notify":
        d = d or {}
        return cls(
            to=str(d.get("to", "") or _env("MAIL_TO", "")),
            gws_from=str(d.get("gws_from", "") or _env("GWS_FROM", "")),
            min_interval_min=float(d.get("min_interval_min", 15.0)),
            max_per_hour=max(1, int(d.get("max_per_hour", 5))),
        )

    @property
    def configured(self) -> bool:
        return bool(self.to)


@dataclass
class Filters:
    names: list[str] = field(default_factory=list)        # 课程名（子串匹配）
    categories: list[str] = field(default_factory=list)   # 课程类别（子串匹配，如 通识核心课I类）
    depts: list[str] = field(default_factory=list)        # 开课单位（子串匹配，如 英语语言文学系）
    match: str = "any"                                    # any=任一命中 / all=全部命中

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> "Filters":
        d = d or {}
        return cls(
            names=list(d.get("names", [])),
            categories=list(d.get("categories", [])),
            depts=list(d.get("depts", [])),
            match=str(d.get("match", "any")),
        )

    @property
    def empty(self) -> bool:
        return not (self.names or self.categories or self.depts)

    @property
    def active_groups(self) -> list[tuple[str, list[str]]]:
        out = []
        if self.names:
            out.append(("课程名", self.names))
        if self.categories:
            out.append(("课程类别", self.categories))
        if self.depts:
            out.append(("开课院系", self.depts))
        return out

    def to_dict(self) -> dict:
        return {"names": self.names, "categories": self.categories,
                "depts": self.depts, "match": self.match}


@dataclass
class Config:
    interval_min: float = 8.0              # 轮询基本间隔（分钟），实际带随机抖动
    interval_jitter: float = 0.3           # 抖动比例（±30%）
    page_delay_min: float = 0.8           # 相邻操作随机间隔下限（秒，人类速度）
    page_delay_max: float = 2.0            # 相邻操作随机间隔上限（秒）
    credentials: Credentials = field(default_factory=Credentials)
    filters: Filters = field(default_factory=Filters)
    notify: Notify = field(default_factory=Notify)
    session: str = "courser-watch"         # opencli 浏览器会话名
    window: str = "background"             # background=后台窗口，不抢焦点
    force_relogin: bool = True             # 每轮先登出再重新登录
    first_run_done: bool = False           # 初次启动向导是否已完成
    cli_log: bool = False                  # CLI 模式（无 TUI）

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        path = path or CONFIG_PATH
        cfg = cls()
        if path.exists():
            try:
                d = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                d = {}
            cfg.interval_min = float(d.get("interval_min", cfg.interval_min))
            cfg.interval_jitter = float(d.get("interval_jitter", cfg.interval_jitter))
            cfg.page_delay_min = float(d.get("page_delay_min", cfg.page_delay_min))
            cfg.page_delay_max = float(d.get("page_delay_max", cfg.page_delay_max))
            cfg.session = str(d.get("session", cfg.session))
            cfg.window = str(d.get("window", cfg.window))
            cfg.force_relogin = bool(d.get("force_relogin", cfg.force_relogin))
            cfg.first_run_done = bool(d.get("first_run_done", cfg.first_run_done))
            cfg.credentials = Credentials.from_dict(d.get("credentials"))
            cfg.filters = Filters.from_dict(d.get("filters"))
            cfg.notify = Notify.from_dict(d.get("notify"))
        return cfg

    def save(self, path: Optional[Path] = None) -> None:
        path = path or CONFIG_PATH
        d = {
            "interval_min": self.interval_min,
            "interval_jitter": self.interval_jitter,
            "page_delay_min": self.page_delay_min,
            "page_delay_max": self.page_delay_max,
            "session": self.session,
            "window": self.window,
            "force_relogin": self.force_relogin,
            "first_run_done": self.first_run_done,
            "credentials": {"username": self.credentials.username,
                            "password": self.credentials.password},
            "filters": self.filters.to_dict(),
            "notify": {"to": self.notify.to, "gws_from": self.notify.gws_from,
                       "min_interval_min": self.notify.min_interval_min,
                       "max_per_hour": self.notify.max_per_hour},
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")

    @property
    def pacing(self) -> tuple[float, float]:
        return (self.page_delay_min, self.page_delay_max)


def load_env_file(path: Optional[Path] = None) -> None:
    """加载项目根目录 .env（若存在），用于填充 PKU 凭据、GWS_FROM 等环境变量。"""
    path = path or (PROJECT_ROOT / ".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())