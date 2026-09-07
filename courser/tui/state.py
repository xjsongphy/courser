"""纯 UI 状态（view state），与渲染/按键解耦。

把原先散落在 CourserApp 上的一堆 `self.*` 归并成语义化的状态 bundle，
让「状态机」可独立测试、可重置，也让 app.py 的字段显著减少。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class MainViewState:
    """主页：课程视图、光标位置、滚动顶行、按列查找。"""

    view: str = "all"          # all | matched | seats
    index: int = 0             # 当前选中行（在可见行中的下标）
    top: int = 0               # 可视窗口顶行（在可见行中的下标）
    snapshot_ts: Optional[str] = None
    snapshot_meta: str = ""
    search_col: Optional[str] = None   # 当前查找列（page/no/name/cat/…）；None = 未启用
    search_query: str = ""             # 已生效的查找词（编辑中取 FieldEditor 缓冲）

    def reset_cursor(self) -> None:
        self.index = 0
        self.top = 0


@dataclass
class ProgressState:
    """抓取进度（本轮步骤/总数/当前操作；total 未知前为 None）。"""

    done: Optional[int] = None
    total: Optional[int] = None
    op: str = ""

    def clear(self, op: str = "本轮完成") -> None:
        self.done = None
        self.total = None
        self.op = op


@dataclass
class FilterViewState:
    """筛选页：维度、搜索串、光标、滚动顶行、是否自动抓取候选。"""

    dim: int = 0               # 索引进 GROUPS
    query: str = ""            # 顶部输入即筛
    index: int = 0
    top: int = 0
    auto_gather: bool = False  # 进筛选页不再自动抓取生成候选（用缓存/手动抓）
    gathering: bool = False    # 正在自动抓取候选


@dataclass
class SettingsViewState:
    """设置页：行索引（draft 事务）。"""

    index: int = 0


@dataclass
class EditingState:
    """行内编辑态：settings / setup / search 共用一个 FieldEditor，这里记录上下文。"""

    context: Optional[str] = None    # None / settings / setup / search
    key: Optional[str] = None        # 当前编辑的字段 key
