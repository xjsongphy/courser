"""单轮完整业务逻辑：抓取 → 风控 → 筛选 → 通知决策 → 结果。

与调度解耦：RoundRunner 只负责「执行一轮」。无论是定时轮询、手动触发（TUI 按 r）、
还是 `courser --once`，都走同一个 `runner.run_round()`。

上一版把持久化/线程/调度全塞在 Watcher 里；这里只保留一轮的编排：
    fetch（注入或默认） → risk 评估 → filter 筛选 → 通知决策（去重/预算） → RoundResult
每个 RoundResult 都会带 duration_s、更新 last_result、触发 on_round ——
包括失败路径（修掉旧代码失败即提前 return、duration 恒 0、last_result 不变的问题）。
"""

from __future__ import annotations

import random
import threading
import time
from typing import Callable, Optional

from . import notifier
from .filters import FilterSet
from .models import Course, FetchResult, RoundResult
from .pku import client
from .risk import BotRisk
from .storage import NotificationStateStore, SendBudgetStore


class RoundRunner:
    """执行一轮抓取 + 筛选 + 通知决策。线程安全（手动一轮与定时轮询互斥）。"""

    def __init__(self, cfg, log: Callable[[str], None],
                 on_round: Optional[Callable[[RoundResult], None]] = None,
                 on_progress: Optional[Callable[[int, Optional[int], str], None]] = None,
                 notify_store: Optional[NotificationStateStore] = None,
                 budget_store: Optional[SendBudgetStore] = None):
        self.cfg = cfg
        self.log = log
        self.on_round = on_round or (lambda r: None)
        self.on_progress = on_progress
        self.risk = BotRisk()
        self.retry_delay_range = (20.0, 40.0)  # 整轮抓取失败后的重试等待（秒，可覆写）
        self.notify_store = notify_store or NotificationStateStore()
        self.budget_store = budget_store or SendBudgetStore()
        self._round_lock = threading.Lock()
        self._notify_lock = threading.Lock()
        self.last_result: Optional[RoundResult] = None
        self.current_round_started_at: Optional[float] = None  # 用于「本轮已用时」实时显示

    # -- 通知去重（同课冷却） --------------------------------------------
    def _should_notify(self, c: Course) -> bool:
        st = self.notify_store.get(c.key)
        prev_avail = st.get("avail", -1)
        last_ts = st.get("ts", 0.0)
        now = time.time()
        cooldown = self.cfg.notify.min_interval_min * 60
        if c.avail <= 0:
            return False
        # 首次出现空余 / 空余变多 / 超过冷却期未通知
        if prev_avail < 0 or c.avail > prev_avail:
            return True
        return (now - last_ts) > cooldown

    # -- 通知决策：同课冷却去重 → 每小时预算 → 组装邮件发送 --------------
    def _notify_seats(self, seats: list[Course]) -> list:
        with self._notify_lock:
            notifiable = [c for c in seats if self._should_notify(c)]
            if not notifiable:
                self.log("无可新通知课程（同课冷却期内）")
                return []
            if not self.budget_store.budget_ok(self.cfg.notify.max_per_hour):
                self.log(f"⚠ 已达每小时发送上限（{self.cfg.notify.max_per_hour} 封），"
                         f"本轮跳过发送；查询/翻页不受影响")
                return []
            for c in notifiable:
                self.notify_store.mark(c)
            self.notify_store.save()
            self.budget_store.record()
        subject = notifier.build_subject(notifiable)
        text, html_body = notifier.build_body(notifiable,
                                              time.strftime("%Y-%m-%d %H:%M:%S"))
        notifier.send_email_with_retry(self.cfg.notify, subject, text,
                                       body_html=html_body, log=self.log)
        return notifiable

    # -- 一轮 ------------------------------------------------------------
    def run_round(self, fetch_round: Optional[Callable] = None) -> RoundResult:
        with self._round_lock:  # 手动触发一轮与定时轮询互斥
            self.current_round_started_at = time.time()
            try:
                return self._run_round(fetch_round=fetch_round)
            finally:
                self.current_round_started_at = None

    def _run_round(self, fetch_round: Optional[Callable] = None) -> RoundResult:
        """执行完整一轮。fetch_round 可注入（默认真抓取），便于测试。"""
        fetch_round = fetch_round or client.fetch_round
        r = RoundResult(ts=time.time())
        t0 = time.time()
        creds = {"username": self.cfg.credentials.username,
                 "password": self.cfg.credentials.password}
        self.log(f"开始新一轮抓取（登录方式：{'配置凭据' if creds.get('username') else '自动填充'}）…")
        _prog = self.on_progress
        try:
            fr = fetch_round(
                session=self.cfg.session,
                creds=creds,
                window=self.cfg.window,
                pacing=self.cfg.pacing,
                force_logout=self.cfg.force_relogin,
                log=self.log,
                on_progress=_prog,
            )
            # 失败重试机制：整轮失败且非风控提示时，等 20~40 秒重试一次
            # （人类遇到失败也会再试一次；风控命中则绝不重试硬顶）。
            # 但「登录未成功」是确定性失败（账号被拒/会话已失效/填值未触发框架），
            # 原地重试只会重复一整轮慢登录（约 1~3 分钟）纯浪费 —— 不原地重试，
            # 交给下一轮调度重试，并明确提示人工处理。
            if not fr.ok and not fr.warning_hit:
                if fr.error.startswith("登录未成功"):
                    self.log("本轮失败：登录未成功。重试无法解决账号/登录问题，"
                             "本轮不原地重试；请检查配置的学号/密码，或先在 Chrome 手动登录"
                             "一次恢复会话，等待下一轮再试。")
                else:
                    self.log(f"本轮抓取失败：{fr.error}；等待约 "
                             f"{self.retry_delay_range[0]:.0f}~{self.retry_delay_range[1]:.0f} "
                             f"秒后重试一次…")
                    if _prog:
                        _prog(0, None, "本轮失败，等待片刻后重试…")
                    time.sleep(random.uniform(*self.retry_delay_range))
                    fr = fetch_round(
                        session=self.cfg.session,
                        creds=creds,
                        window=self.cfg.window,
                        pacing=self.cfg.pacing,
                        force_logout=self.cfg.force_relogin,
                        log=self.log,
                        on_progress=_prog,
                    )

            r.login_mode = fr.login_mode
            r.pages = fr.pages
            r.total = len(fr.courses)
            r.courses = fr.courses
            r.warning_hit = fr.warning_hit
            if fr.warning_hit:
                self.risk.mark_warning()
                self.log("⚠ 检测到页面出现风控/警告提示语，已判定为高触发率，本轮照常结束但请人工关注")
            r.risk_percent, r.risk_label = self.risk.evaluate(
                pages=r.pages, duration_s=time.time() - t0)

            if not fr.ok:
                r.ok = False
                r.error = fr.error
                self.log(f"✗ 本轮失败：{fr.error}")
                # 注意：不提前 return —— 也要正常收尾 duration/on_round
            else:
                self.log(f"本轮抓取完成：{r.pages} 页，共 {r.total} 门课程；"
                         f"登录方式={fr.login_mode}；风控触发率≈{r.risk_percent}%（{r.risk_label}）")
                if r.warning_hit:
                    self.log("⚠ 建议暂停监控并人工登录一次，恢复后再以更低频率继续")
                fs = FilterSet(self.cfg.filters)
                r.matched = fs.matched(fr.courses)
                seats = [c for c in r.matched if c.has_seats]
                if seats:
                    self.log(f"命中 {len(r.matched)} 门，其中 {len(seats)} 门有空余名额 → 检查通知"
                             f"（每小时发送上限 {self.cfg.notify.max_per_hour} 封）")
                    r.notified = self._notify_seats(seats)
                else:
                    self.log(f"命中 {len(r.matched)} 门，暂无空余名额"
                             + ("" if r.matched else "（且当前筛选条件未命中任何课程）"))
        except Exception as exc:  # noqa: BLE001
            r.ok = False
            r.error = str(exc)
            self.log(f"✗ 本轮异常：{exc}")

        r.duration_s = time.time() - t0
        r.ts = time.time()
        self.last_result = r
        self.on_round(r)
        return r
