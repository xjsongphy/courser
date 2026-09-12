"""周期监控调度：只在后台线程里决定「何时执行下一轮」，并把执行交给 RoundRunner。

职责边界：
- RoundRunner = 执行一轮（抓取 → 筛选 → 通知）
- MonitorScheduler = start / stop / 下一轮时间 / 抖动 / 风控放慢 / 线程生命周期

对外暴露 Watcher 时代兼容的表面（running / countdown_s / last_result / run_round /
start / stop），让 TUI 与 `--once` 无需感知内部拆分。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional

from . import logfile
from .config import Config, RANDOM_BREAK_PROFILES
from .human import jitter
from .models import FetchFailureKind, RoundResult
from .runner import RoundRunner


def _in_night_window(now=None):
    """当前是否处于夜间窗口（0:00 ≤ hour < 6:00）。"""
    now = now or time.localtime()
    return 0 <= now.tm_hour < 6


class MonitorScheduler:
    """后台监控线程：周期调用 RoundRunner 执行一轮。"""

    def __init__(self, cfg: Config, log: Callable[[str], None],
                 on_round: Optional[Callable[[RoundResult], None]] = None,
                 on_progress: Optional[Callable[[int, Optional[int], str], None]] = None,
                 fetch_round: Optional[Callable] = None):
        self.cfg = cfg
        self._fetch_round = fetch_round   # 注入测试/自定义抓取；None=走真实 client.fetch_round
        # 所有日志同时落盘 data/courser.log（TUI/CLI 两模式都覆盖）
        user_log = log

        def chained(msg: str) -> None:
            user_log(msg)
            logfile.log(msg)

        self.log = chained
        self.runner = RoundRunner(cfg, log=self.log,
                                  on_round=on_round, on_progress=on_progress)
        self.on_progress = on_progress
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False
        self.next_round_ts: Optional[float] = None
        self._night_seen = False        # 本轮是否已进入过夜间暂停（避免重复日志）
        self._resume_running = True     # 夜间暂停结束时是否恢复运行（= 进入夜间时是否在运行）
        self._started_in_night = False  # 循环是否在夜间窗口内启动（0 点前未运行 → 6 点不自动恢复）
        self._success_streak = 0        # 连续成功轮次（随机暂停计数）
        self._break_tier = None         # 当前随机暂停档位（变化时重新计数）
        self._break_after = 0           # 本次达到多少成功轮后休息

    # -- 转发给 runner --------------------------------------------------
    def run_round(self, fetch_round: Optional[Callable] = None,
                  cancel_event: Optional[threading.Event] = None) -> RoundResult:
        # 开跑前重读磁盘配置：感知运行期间外部对 config.json 的改动（手动 r 与
        # 周期轮询都走这里）。就地 reload 让 runner 持有同一 cfg 引用同步生效。
        self.cfg.reload()
        return self.runner.run_round(fetch_round or self._fetch_round,
                                     cancel_event=cancel_event)

    @property
    def last_result(self) -> Optional[RoundResult]:
        return self.runner.last_result

    @property
    def risk(self):
        return self.runner.risk

    @property
    def current_round_started_at(self) -> Optional[float]:
        return self.runner.current_round_started_at

    # -- 线程生命周期 ----------------------------------------------------
    def _loop(self) -> None:
        self.running = True
        self._night_seen = False
        while not self._stop.is_set():
            self.cfg.reload()
            # 夜间暂停：0:00~6:00 期间不执行轮次，仅等 6 点（或 night_pause 被关 / 收到停止）。
            # 6 点后按「进入夜间时」的运行态恢复：跨夜运行才恢复；0 点前未运行则保持暂停。
            if self.cfg.night_pause and _in_night_window():
                if not self._night_seen:
                    self._night_seen = True
                    self._resume_running = not self._started_in_night
                    self.next_round_ts = self._next_morning_ts()
                    self.log("已进入夜间暂停（0:00~6:00）：暂停抓取，6 点后按 0 点前状态恢复")
                if self._suppress_until_morning():
                    break          # 收到停止信号
                self._night_seen = False
                if not self._resume_running:
                    self.running = False
                    self.log("夜间暂停结束：0 点前未运行，不自动恢复；保持暂停，可手动重新开始")
                    break
                self.log("夜间暂停结束，恢复监控")
                continue
            self.run_round(cancel_event=self._stop)
            if self._stop.is_set():
                break
            last = self.last_result
            if last and last.failure_kind == FetchFailureKind.BROWSER_UNAVAILABLE:
                # 浏览器桥/Chrome 根本不可达：再等下一轮只会重复同样的慢登录/超时，
                # 且原地重试也已显式禁止——直接停止监控，提示先修环境。
                self.log("✗ OpenCLI/Chrome 浏览器桥不可用：停止监控；"
                         "请确保 Chrome/opencli 已启动后再重新开始")
                break
            break_secs = self._maybe_break(self.last_result)
            if break_secs:
                self.next_round_ts = time.time() + break_secs
                self.log(f"⚠ 随机暂停：已连续完成多轮，休息约 {break_secs / 60:.1f} 分钟")
                self._wait_sleep(break_secs)
                continue
            wait = self._next_wait(self.last_result)
            if self.last_result and self.last_result.warning_hit:
                self.log(f"⚠ 本轮命中风控提示，已放慢节奏：约 {wait / 60:.1f} 分钟后下一轮")
            self.next_round_ts = time.time() + wait
            self.log(f"本轮结束，约 {wait / 60:.1f} 分钟后开始下一轮")
            self._wait_sleep(wait)
        self.running = False

    def _maybe_break(self, last: Optional[RoundResult]) -> Optional[float]:
        """随机暂停决策（工作窗口模型）：按档位成功轮计数，达到目标就返回应休息秒数。

        档位映射见 config.RANDOM_BREAK_PROFILES；返回非空表示本轮结束后进入随机休息
        （调用方负责 sleep）；返回 None 则走正常间隔。失败/取消/关闭 → 计数清零。
        """
        tier = self.cfg.random_break
        profile = RANDOM_BREAK_PROFILES.get(tier)
        if profile is None or not (last and last.ok and not last.cancelled):
            self._success_streak = 0
            self._break_tier = None
            return None
        rounds_range, dur_range = profile
        if self._break_tier != tier:      # 首次开启/换档 → 重新开始计数
            self._break_tier = tier
            self._success_streak = 0
            self._break_after = random.randint(*rounds_range)
        self._success_streak += 1
        if self._success_streak < self._break_after:
            return None
        self._success_streak = 0
        self._break_after = random.randint(*rounds_range)
        return random.uniform(*dur_range) * 60

    def _wait_sleep(self, wait: float) -> None:
        """分段 sleep，便于及时响应停止；wind 到点即停。"""
        deadline = time.time() + wait
        while time.time() < deadline and not self._stop.is_set():
            time.sleep(1.0)

    def _next_wait(self, last: Optional[RoundResult]) -> float:
        """下一轮等待时间：**始终以基准间隔为底**，风控放慢只乘一次固定系数。

        绝不累积——即使用户连续多轮命中风控，每轮都还是 `base × 4`（抖动 0.3），
        而不是把上一轮的 wait 再放大（那会指数爆炸）。以后改这里务必保持不变式：
        warning 轮 wait ∈ [0.7×4×base, 1.3×4×base]，正常轮 ∈ [抖动 base]。
        """
        base = self.cfg.interval_min * 60
        if last and last.warning_hit:
            return jitter(base * 4.0, 0.3)
        return jitter(base, self.cfg.interval_jitter)

    def _suppress_until_morning(self) -> bool:
        """夜间暂停等待循环：每秒检查，直到 6 点 / night_pause 被关掉 / 收到停止。
        返回 True 表示应结束本轮监控（收到停止）。"""
        while not self._stop.is_set():
            self.cfg.reload()
            if not self.cfg.night_pause or not _in_night_window():
                return False
            time.sleep(1.0)
        return True

    def _next_morning_ts(self) -> float:
        """今天 6:00 的时间戳（夜间窗口内调用，必然在今天）。"""
        now = time.localtime()
        return time.mktime((now.tm_year, now.tm_mon, now.tm_mday,
                            6, 0, 0, 0, 0, -1))

    @property
    def night_pause_active(self) -> bool:
        """当前是否处于夜间暂停（开启且处于夜间窗口且监控在运行）。"""
        return bool(self.cfg.night_pause) and _in_night_window() and self.running

    @property
    def night_resume_ts(self) -> Optional[float]:
        return self._next_morning_ts() if self.night_pause_active else None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        # 夜间窗口内启动：0 点前未运行 → 6 点不自动恢复（用户明确要求）
        self._started_in_night = _in_night_window() and self.cfg.night_pause
        self._thread = threading.Thread(target=self._loop, daemon=True, name="courser-watcher")
        self._thread.start()

    def stop(self) -> None:
        self.request_stop()
        if self._thread:
            self._thread.join(timeout=5)
        self.running = False

    def request_stop(self) -> None:
        """立即发出停止信号；调用方可选择稍后再 join 后台线程。"""
        self._stop.set()
        self.runner.cancel_current_round()

    @property
    def countdown_s(self) -> Optional[int]:
        if self.running and self.next_round_ts:
            return max(0, int(self.next_round_ts - time.time()))
        return None
