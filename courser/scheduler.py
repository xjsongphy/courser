"""周期监控调度：只在后台线程里决定「何时执行下一轮」，并把执行交给 RoundRunner。

职责边界：
- RoundRunner = 执行一轮（抓取 → 筛选 → 通知）
- MonitorScheduler = start / stop / 下一轮时间 / 抖动 / 风控放慢 / 线程生命周期

对外暴露 Watcher 时代兼容的表面（running / countdown_s / last_result / run_round /
start / stop），让 TUI 与 `--once` 无需感知内部拆分。
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from . import logfile
from .config import Config
from .human import jitter
from .models import FetchFailureKind, RoundResult
from .runner import RoundRunner


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
        while not self._stop.is_set():
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
            last = self.last_result
            if last and last.failure_kind == FetchFailureKind.BROWSER_UNAVAILABLE:
                # 浏览器桥/Chrome 根本不可达：再等下一轮只会重复同样的慢登录/超时，
                # 且原地重试也已显式禁止——直接停止监控，提示先修环境。
                self.log("✗ OpenCLI/Chrome 浏览器桥不可用：停止监控；"
                         "请确保 Chrome/opencli 已启动后再重新开始")
                break
            wait = self._next_wait(self.last_result)
            if self.last_result and self.last_result.warning_hit:
                self.log(f"⚠ 本轮命中风控提示，已放慢节奏：约 {wait / 60:.1f} 分钟后下一轮")
            self.next_round_ts = time.time() + wait
            self.log(f"本轮结束，约 {wait / 60:.1f} 分钟后开始下一轮")
            # 分段 sleep，便于及时响应停止
            deadline = time.time() + wait
            while time.time() < deadline and not self._stop.is_set():
                time.sleep(1.0)
        self.running = False

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

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
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
