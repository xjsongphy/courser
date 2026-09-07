"""监控循环。

每一轮：登出 → 重新登录 → 进入补退选 → 动态翻页抓取 → 筛选 → 有空余名额且
命中筛选的课程触发邮件通知（带去重与冷却，避免刷屏）。

节奏：翻页等相邻操作随机间隔（page_delay_min~max 秒），轮询间隔按
interval_min ± jitter 随机抖动 —— 模仿人类、避免风控。
出错（登录失败/需要验证码）时不重试硬顶，降速等待下一轮并在日志中提示人工介入。
"""

from __future__ import annotations

import json
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import fetch, logfile, notifier, risk as riskmod
from .config import Config, SEND_LOG_FILE, STATE_FILE
from .filters import FilterSet
from .human import jitter


@dataclass
class RoundResult:
    ts: float = 0.0
    ok: bool = True
    error: str = ""
    login_mode: str = ""
    pages: int = 0
    total: int = 0
    courses: list = field(default_factory=list)
    matched: list = field(default_factory=list)
    notified: list = field(default_factory=list)
    duration_s: float = 0.0
    warning_hit: bool = False        # 页面检测到风控提示语
    risk_percent: int = 0            # 刷课机警告触发率估计（0~100）
    risk_label: str = "无"


class Watcher:
    """后台监控线程：周期执行一轮抓取。"""

    def __init__(self, cfg: Config, log: Callable[[str], None],
                 on_round: Optional[Callable[[RoundResult], None]] = None):
        self.cfg = cfg
        # 所有日志同时落盘 data/courser.log（TUI/CLI 两模式都覆盖）
        user_log = log

        def chained(msg: str) -> None:
            user_log(msg)
            logfile.log(msg)

        self.log = chained
        self.on_round = on_round or (lambda r: None)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.running = False
        self.next_round_ts: Optional[float] = None
        self.last_result: Optional[RoundResult] = None
        self._state: dict = self._load_state()
        self._lock = threading.Lock()
        self._round_lock = threading.Lock()
        self.risk = riskmod.BotRisk()
        self.retry_delay_range = (20.0, 40.0)  # 整轮抓取失败后的重试等待（秒，可覆写）

    # -- 状态持久化（课程 seq -> 上次空余 / 上次通知时间） -----------------
    def _load_state(self) -> dict:
        try:
            if STATE_FILE.exists():
                return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        return {}

    def _save_state(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            STATE_FILE.write_text(json.dumps(self._state, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
        except Exception:
            pass

    # -- 通知去重 --------------------------------------------------------
    def _should_notify(self, c) -> bool:
        key = c.key
        st = self._state.get(key, {})
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

    def _mark_notified(self, c) -> None:
        self._state[c.key] = {"avail": c.avail,
                              "quota": c.quota,
                              "selected": c.selected,
                              "ts": time.time(),
                              "name": c.name,
                              "seq": c.seq,
                              "dept": c.dept,
                              "category": c.category}

    # -- 每小时发送上限（只限发信，不影响查询轮次） ---------------------
    def _send_log(self) -> list[float]:
        try:
            if SEND_LOG_FILE.exists():
                return [float(t) for t in json.loads(SEND_LOG_FILE.read_text(encoding="utf-8"))]
        except Exception:
            pass
        return []

    def _budget_ok(self) -> bool:
        now = time.time()
        recent = [t for t in self._send_log() if now - t < 3600]
        return len(recent) < self.cfg.notify.max_per_hour

    def _record_send(self) -> None:
        now = time.time()
        recent = [t for t in self._send_log() if now - t < 3600]
        recent.append(now)
        try:
            SEND_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
            SEND_LOG_FILE.write_text(json.dumps(recent), encoding="utf-8")
        except Exception:
            pass

    # -- 通知决策：同课冷却去重 → 每小时预算 → 组装邮件发送 --------------
    def _notify_seats(self, seats: list) -> list:
        with self._lock:
            notifiable = [c for c in seats if self._should_notify(c)]
            if not notifiable:
                self.log("无可新通知课程（同课冷却期内）")
                return []
            if not self._budget_ok():
                self.log(f"⚠ 已达每小时发送上限（{self.cfg.notify.max_per_hour} 封），"
                         f"本轮跳过发送；查询/翻页不受影响")
                return []
            for c in notifiable:
                self._mark_notified(c)
            self._save_state()
            self._record_send()
        subject = notifier.build_subject(notifiable)
        text, html_body = notifier.build_body(notifiable,
                                              time.strftime("%Y-%m-%d %H:%M:%S"))
        notifier.send_email(self.cfg.notify, subject, text, body_html=html_body, log=self.log)
        return notifiable

    # -- 一轮 ------------------------------------------------------------
    def run_round(self, fetch_round: Optional[Callable] = None) -> RoundResult:
        with self._round_lock:  # 手动触发一轮与定时轮询互斥
            return self._run_round(fetch_round=fetch_round)

    def _run_round(self, fetch_round: Optional[Callable] = None) -> RoundResult:
        """执行完整一轮。fetch_round 可注入（默认真抓取），便于测试。"""
        fetch_round = fetch_round or fetch.fetch_round
        r = RoundResult(ts=time.time())
        t0 = time.time()
        creds = {"username": self.cfg.credentials.username,
                 "password": self.cfg.credentials.password}
        self.log(f"开始新一轮抓取（登录方式：{'配置凭据' if creds.get('username') else '自动填充'}）…")
        try:
            fr = fetch_round(
                session=self.cfg.session,
                creds=creds,
                window=self.cfg.window,
                pacing=self.cfg.pacing,
                force_logout=self.cfg.force_relogin,
                log=self.log,
            )
            # 失败重试机制：整轮失败且非风控提示时，等 20~40 秒重试一次
            # （人类遇到失败也会再试一次；风控命中则绝不重试硬顶）
            if not fr.ok and not fr.warning_hit:
                self.log(f"本轮抓取失败：{fr.error}；等待约 "
                         f"{self.retry_delay_range[0]:.0f}~{self.retry_delay_range[1]:.0f} "
                         f"秒后重试一次…")
                time.sleep(random.uniform(*self.retry_delay_range))
                fr = fetch_round(
                    session=self.cfg.session,
                    creds=creds,
                    window=self.cfg.window,
                    pacing=self.cfg.pacing,
                    force_logout=self.cfg.force_relogin,
                    log=self.log,
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
                return r
            r.total = len(fr.courses)
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

    # -- 线程生命周期 ----------------------------------------------------
    def _loop(self) -> None:
        self.running = True
        while not self._stop.is_set():
            self.run_round()
            if self._stop.is_set():
                break
            base = self.cfg.interval_min * 60
            if self.last_result and self.last_result.warning_hit:
                # 检测到风控提示：下一轮等待时间放大 3~5 倍，明显放慢节奏
                wait = jitter(base * 4.0, 0.3)
                self.log(f"⚠ 本轮命中风控提示，已放慢节奏：约 {wait / 60:.1f} 分钟后下一轮")
            else:
                wait = jitter(base, self.cfg.interval_jitter)
            self.next_round_ts = time.time() + wait
            self.log(f"本轮结束，约 {wait / 60:.1f} 分钟后开始下一轮")
            # 分段 sleep，便于及时响应停止
            deadline = time.time() + wait
            while time.time() < deadline and not self._stop.is_set():
                time.sleep(1.0)
        self.running = False

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="courser-watcher")
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=5)
        self.running = False

    @property
    def countdown_s(self) -> Optional[int]:
        if self.running and self.next_round_ts:
            return max(0, int(self.next_round_ts - time.time()))
        return None