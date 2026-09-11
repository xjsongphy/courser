"""领域模型（domain objects）。

全系统共用的数据对象，不归属任何具体实现（抓取 / 监控 / UI 都不该自己定义它们）。
- Course      补退选列表中的一门课程
- FetchResult 一次抓取（一轮登录 + 翻页）的原始结果
- RoundResult 一轮完整监控（抓取 → 筛选 → 通知决策）的最终结果
- FetchError / LoginError  抓取流程异常
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Optional


# 同一门课可同时属于多类（如「任选、思政选择性必修」），类别格内用分隔符并列
_CATEGORY_SPLIT_RE = re.compile(r"[、,，/；;]")

# 表格脚部分页栏文本特征：这些行会被误判成 Course，必须剔除
_PAGER_MARKERS = ("page", "previous", "next", "跳到", "dopagersubmit",
                  "first /", "/ last")


class FetchError(RuntimeError):
    """抓取流程中的一般错误。"""


class LoginError(FetchError):
    """登录失败（可能需要验证码/二次验证，或账号问题）。"""


class FetchFailureKind(Enum):
    """供调度层使用的结构化失败原因，避免解析中文错误字符串。"""

    AUTH_FAILED = auto()
    AUTH_EXPIRED = auto()
    CAPTCHA = auto()
    RISK_BLOCKED = auto()
    PAGE_UNKNOWN = auto()
    BROWSER_ERROR = auto()


@dataclass
class Course:
    """补退选列表中的一门课程（仅可用课程列表，不含已选课程）。"""

    course_no: str = ""
    name: str = ""
    category: str = ""
    credits: str = ""
    weekly_hours: str = ""
    teacher: str = ""
    class_no: str = ""
    dept: str = ""
    grade: str = ""
    schedule: str = ""
    pnp: str = ""
    seats_raw: str = ""
    quota: Optional[int] = None      # 限数
    selected: Optional[int] = None   # 已选
    avail: int = -1                  # 空余 = 限数 - 已选；-1 表示未知
    status: str = ""                 # 选课状态：可申请 / 不可申请 / 已选上 / (空)
    seq: str = ""                    # 课程稳定 id（course_seq_no）
    links: dict = field(default_factory=dict)
    page: int = 0                    # 在选课网列表中的页码（1 起）

    @property
    def categories(self) -> list[str]:
        """该课的独立课程类别。同一门课可同时属于多类，
        如「任选、思政选择性必修」拆成 ['任选', '思政选择性必修'] 两类。"""
        return [p for p in (s.strip() for s in _CATEGORY_SPLIT_RE.split(self.category)) if p]

    @property
    def has_seats(self) -> bool:
        return self.avail > 0

    @property
    def key(self) -> str:
        return self.seq or f"{self.course_no}#{self.class_no}"


@dataclass
class FetchResult:
    courses: list[Course] = field(default_factory=list)
    pages: int = 0
    login_mode: str = ""          # login_click / sso_auto
    ok: bool = True
    error: str = ""
    failure_kind: Optional[FetchFailureKind] = None
    cancelled: bool = False       # 用户主动停止本轮，不属于抓取故障
    warning_hit: bool = False     # 页面文本中检测到风控/警告提示语
    warning_checked: bool = False  # 本轮是否真正进入了补退选页面、并读取了至少一页文本
                                 # （有观察风控警告的机会，才计入风控触发率分母）


def is_real_course(c: "Course") -> bool:
    """剔除被误当课程的表格脚/分页栏：无课程号，或文本带分页栏特征。

    选课网可用列表每页末尾有一行「Page X of Y · First/Previous/Next/Last ·
    跳到：function doPagerSubmit(...)」的页脚，会被 parse_course 读成一门伪课程，
    其字符串含 JS 文本，渲染时可能触发 markup 解析崩溃。这里做兜底过滤。
    """
    no = (c.course_no or "").strip()
    if not no:
        return False
    text = (f"{no} {c.name or ''}").lower()
    return not any(m in text for m in _PAGER_MARKERS)


@dataclass
class RoundResult:
    ts: float = 0.0
    ok: bool = True
    error: str = ""
    cancelled: bool = False       # 用户主动停止本轮，不显示为抓取失败
    login_mode: str = ""
    pages: int = 0
    total: int = 0
    courses: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    notified: list = field(default_factory=list)
    duration_s: float = 0.0
    warning_hit: bool = False        # 页面检测到风控提示语
    risk_percent: int = 0            # 最近 N 次有效抓取中实际警告触发率（0~100）
    risk_label: str = "无"
    risk_hits: int = 0               # 最近窗口内触发警告的有效抓取次数
    risk_total: int = 0              # 最近窗口内的有效抓取总次数
