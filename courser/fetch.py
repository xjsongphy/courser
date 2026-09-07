"""抓取补退选可用课程列表（只读）。

流程（每一轮监控都会完整执行）：
1. 退出旧会话（logout.do + iaaa logout.jsp，best-effort）
2. 打开 IAAA OAuth 登录页
   - 若已配置用户名/密码 → 依次 fill 后点登录
   - 否则 → 等待密码管理器自动填充（1~2s），直接点登录
   - 不做任何验证码输入；出现验证码/错误 → 抛 LoginError，由上层降速暂停
3. 点击菜单「补退选」进入补退选页
4. 动态翻页（每次解析 "Page X of Y" 分页器，不固定页数），
   逐页只读提取可用课程列表中的 限数/已选 等字段
"""

from __future__ import annotations

import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from . import opencli as oc
from .human import sleep_rand

LOGIN_URL = (
    "https://iaaa.pku.edu.cn/iaaa/oauth.jsp?appID=syllabus"
    "&appName=%E5%AD%A6%E7%94%9F%E9%80%89%E8%AF%BE%E7%B3%BB%E7%BB%9F"
    "&redirectUrl=http://elective.pku.edu.cn:80/elective2008/ssoLogin.do"
)
ELECTIVE_BASE = "https://elective.pku.edu.cn"
LOGOUT_URL = ELECTIVE_BASE + "/elective2008/logout.do"
# 注意：iaaa.pku.edu.cn/iaaa/logout.jsp 不存在（返回 404），不要用它；
# elective 的 logout.do 已足够（会登出并重定向到 IAAA 登录页）。

_SEATS_RE = re.compile(r"(\d+)\s*[/／]\s*(\d+)")


class FetchError(RuntimeError):
    """抓取流程中的一般错误。"""


class LoginError(FetchError):
    """登录失败（可能需要验证码/二次验证，或账号问题）。"""


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
    warning_hit: bool = False     # 页面文本中检测到风控/警告提示语


# ---------------------------------------------------------------------------
# 提取 JS（只读；返回 JSON 字符串）
# ---------------------------------------------------------------------------

_EXTRACT_JS = r"""
(() => {
  const norm = s => (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim();
  const isHeader = cs => cs.some(c => c.includes('课程号'))
      && cs.some(c => c.includes('课程名'))
      && cs.some(c => c.includes('限数'));
  function parseTable(tbl) {
    const rows = [...tbl.querySelectorAll('tr')];
    let hi = -1, header = [];
    for (let i = 0; i < rows.length; i++) {
      const cs = [...rows[i].querySelectorAll('th,td')].map(c => norm(c.textContent));
      if (isHeader(cs)) { hi = i; header = cs; break; }
    }
    if (hi < 0) return null;
    const data = [];
    for (let i = hi + 1; i < rows.length; i++) {
      const tds = [...rows[i].querySelectorAll('td')];
      if (!tds.length) continue;
      const cells = tds.map(c => norm(c.textContent));
      if (cells.every(c => c === '')) continue;
      const links = [...rows[i].querySelectorAll('a')]
        .map(a => ({ t: norm(a.textContent), h: a.getAttribute('href') || '' }))
        .filter(l => l.t || l.h);
      data.push({ cells, links });
    }
    return { header, rows: data };
  }
  const rawTables = [...document.querySelectorAll('table')];
  const next = [...document.querySelectorAll('a')].find(a => norm(a.textContent) === 'Next');
  const tables = rawTables
    .map(el => { const p = parseTable(el); return p ? { el, ...p } : null; })
    .filter(Boolean);
  const body = document.body.innerText || '';
  const pm = body.match(/Page\s+(\d+)\s+of\s+(\d+)/i);
  return JSON.stringify({
    url: location.href,
    pager: pm ? { cur: +pm[1], total: +pm[2] } : null,
    has_next: !!next,
    next_href: next ? next.getAttribute('href') : null,
    // 仅当页面连课程表都没有时才判定风控/警告（选课页常驻"请勿使用刷课机"
    // 警示条，不能因为静态文案就误报）
    warning: tables.length === 0 && /(刷课机|过于频繁|频率过高|操作频繁|风控|异常访问|请勿使用)/.test(body),
    tables: tables.map(({ el, ...rest }) => ({ ...rest,
                                               pager_here: next ? el.contains(next) : false }))
  });
})()
"""


class Progress:
    """抓取进度：单调递增的步骤计数 + 当前操作描述 + 估算总步数。

    sink(done, total, op) —— total 未知前为 None，页数翻出后变为稳定分母。
    """

    def __init__(self, sink: Optional[Callable[[int, Optional[int], str], None]] = None):
        self.sink = sink or (lambda *_: None)
        self.done = 0
        self.total: Optional[int] = None

    def step(self, op: str, total: Optional[int] = None) -> None:
        self.done += 1
        if total is not None:
            self.total = total
        try:
            self.sink(self.done, self.total, op)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 页面解析
# ---------------------------------------------------------------------------

def _col(header: list[str], *keys: str) -> Optional[int]:
    for i, h in enumerate(header):
        if any(k in h for k in keys):
            return i
    return None


def _parse_course(header: list[str], cells: list[str], links: list[dict]) -> Course:
    c = Course()
    i_no = _col(header, "课程号")
    i_name = _col(header, "课程名")
    i_cat = _col(header, "课程类别")
    i_credit = _col(header, "学分")
    i_hours = _col(header, "周学时")
    i_teacher = _col(header, "教师")
    i_class = _col(header, "班号")
    i_dept = _col(header, "开课单位")
    i_grade = _col(header, "年级")
    i_sched = _col(header, "上课", "考试")
    i_pnp = _col(header, "P/NP")
    i_seats = _col(header, "限数")
    i_status = _col(header, "选课状态")

    def cell(i: Optional[int]) -> str:
        return cells[i] if i is not None and i < len(cells) else ""

    c.course_no, c.name = cell(i_no), cell(i_name)
    c.category, c.credits, c.weekly_hours = cell(i_cat), cell(i_credit), cell(i_hours)
    c.teacher, c.class_no, c.dept, c.grade = cell(i_teacher), cell(i_class), cell(i_dept), cell(i_grade)
    c.schedule, c.pnp = cell(i_sched), cell(i_pnp)
    c.seats_raw = cell(i_seats)
    m = _SEATS_RE.search(c.seats_raw)
    if m:
        c.quota, c.selected = int(m.group(1)), int(m.group(2))
        c.avail = c.quota - c.selected
    c.status = cell(i_status)

    for l in links:
        h = l["h"] or ""
        if "goNested.do" in h:
            c.links["detail"] = h
            q = urllib.parse.parse_qs(urllib.parse.urlparse(h).query)
            if "course_seq_no" in q:
                c.seq = urllib.parse.unquote(q["course_seq_no"][0])
        elif "electSupplement.do" in h:
            c.links.setdefault("elect", []).append(h)   # 补选/刷新入口；监控时绝不点击
        elif "cancelCourse.do" in h:
            c.links["drop"] = h                          # 退选入口；同样只记录
    if not c.status and "elect" in c.links:
        c.status = "可申请" if c.links.get("elect_text") == "补选" else ""
    # 有些行以链接文本表达可申请/不可申请
    for l in links:
        if l["t"] in ("补选", "刷新") and "electSupplement.do" in (l["h"] or ""):
            c.status = c.status or ("可申请" if l["t"] == "补选" else "不可申请")
    return c


def _pick_electable_table(tables: list[dict]) -> Optional[dict]:
    """挑出真正的「补退选可用列表」表格。

    页面可能同时出现：外层包裹表（表头长度异常）、已选上列表、可用列表。
    判定依据（由强到弱）：
    1. 表头长度在 10~16 之间（排除把整页包进去的畸形表）；
    2. 该表内包含翻页用的 Next 链接（分页只属于可用列表）；
    3. 含 electSupplement.do（补选/刷新）链接的行数最多。
    """
    sane = [t for t in tables if 10 <= len(t.get("header") or []) <= 16]
    if not sane:
        sane = tables
    for t in sane:
        if t.get("pager_here"):
            return t
    best, best_score = None, -1
    for t in sane:
        score = sum(
            1 for r in (t.get("rows") or [])
            if any("electSupplement" in (l.get("h") or "") for l in (r.get("links") or []))
        )
        if score > best_score:
            best, best_score = t, score
    return best if best_score > 0 else (sane[0] if sane else None)


def _sig(page_courses: list[Course]) -> str:
    """本页课程签名（用于翻页是否生效的判重）。"""
    head = page_courses[:3]
    return "|".join(c.key for c in head) + f"#{len(page_courses)}"


def _parse_page(data: dict) -> tuple[list[Course], dict, bool]:
    """返回 (可用课程列表, 分页信息, 页面是否含风控提示语)。"""
    courses: list[Course] = []
    t = _pick_electable_table(data.get("tables") or [])
    if t is not None:
        header, rows = t.get("header") or [], t.get("rows") or []
        for r in rows:
            courses.append(_parse_course(header, r.get("cells") or [], r.get("links") or []))
    pager = data.get("pager") or {}
    return (courses,
            {"has_next": bool(data.get("has_next")),
             "next_href": data.get("next_href"),
             "cur": (pager or {}).get("cur"),
             "total": (pager or {}).get("total")},
            bool(data.get("warning")))


def _absolute(href: str) -> str:
    return href if href.startswith("http") else ELECTIVE_BASE + href


def _poll_url(session: str, needle: str, timeout_s: float = 30.0,
              log: Optional[Callable[[str], None]] = None) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if needle in oc.get_url(session):
                return True
        except Exception:
            pass
        sleep_rand(1.5, 2.5)
    return False


_TABLE_READY_JS = (
    r"(() => { const tb = [...document.querySelectorAll('table')]; "
    r"for (const t of tb) { const cs = [...t.querySelectorAll('th,td')]"
    r".map(c => (c.textContent || '').trim()); "
    r"if (cs.some(x => x.includes('课程号')) && cs.some(x => x.includes('限数'))) "
    r"return true; } return false; })()"
)


def _table_ready(session: str) -> bool:
    """课程表是否已渲染出来（页面加载未完成时抓取会拿到空数据）。"""
    try:
        return oc.eval_js(session, _TABLE_READY_JS) is True
    except Exception:
        return False


def _wait_table(session: str, timeout_s: float = 20.0,
                log: Optional[Callable[[str], None]] = None) -> bool:
    """等到课程表出现；超时返回 False。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if _table_ready(session):
            return True
        sleep_rand(0.5, 1.0)
    return False


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

def _on_workable_page(session: str) -> bool:
    """确认已登录且进入可用页面：出现补退选菜单链接，或已在补退选页。"""
    try:
        ok = oc.eval_js(
            session,
            r"(() => !!document.querySelector('a[href*=\"SupplyCancel.do\"]') "
            r"|| /SupplyCancel|supplement/i.test(location.href))()",
        )
        return ok is True
    except Exception:
        return False


def _poll_landed(session: str, timeout_s: float = 40.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        sleep_rand(0.8, 1.5)
        if _on_workable_page(session):
            return True
    return False


def _form_present(session: str) -> bool:
    """登录表单是否在位（#logon_button 存在）。页面重定向进行中时可能短暂缺失。"""
    try:
        return oc.eval_js(session, "(() => !!document.querySelector('#logon_button'))()") is True
    except Exception:
        return False


def _click_logon(session: str) -> bool:
    """点击登录按钮，并确认真的点到了（clicked=true 且匹配到按钮）。

    若点击瞬间表单被重定向走/未匹配到 → 返回 False（由上层重试），不抛错。
    """
    try:
        env = oc.click(session, "input#logon_button")
        return bool(env.get("clicked")) and int(env.get("matches_n") or 1) >= 1
    except oc.OpenCliError as exc:
        if "selector_not_found" in (exc.stderr or "") or "matched 0 elements" in (exc.stderr or ""):
            return False
        raise


def _focus_user(session: str) -> bool:
    """点击用户名框并确认焦点真正落在其上（触发密码管理器填充的前提）。"""
    for _ in range(2):
        try:
            oc.click(session, "#user_name")
        except Exception:
            pass
        sleep_rand(0.5, 1.0)
        try:
            focused = oc.eval_js(
                session,
                "(() => !!(document.activeElement && "
                "document.activeElement.id === 'user_name'))()",
            )
            if focused is True:
                return True
        except Exception:
            pass
    return False


def _login_panel_visible(session: str) -> bool:
    """「账号登录」面板是否可见（隐藏时点登录按钮无效）。"""
    try:
        v = oc.eval_js(
            session,
            r"(() => { const el = document.querySelector('#login_panel'); "
            r"if (!el) return false; const cs = getComputedStyle(el); "
            r"return cs.display !== 'none' && el.getBoundingClientRect().height > 0; })()",
        )
        return v is True
    except Exception:
        return False


def _filled_len(session: str) -> tuple[int, int]:
    """返回 (用户名长度, 密码长度)；异常时 (-1, -1)。"""
    try:
        r = oc.eval_js(
            session,
            r"(() => { const u = document.querySelector('#user_name'); "
            r"const p = document.querySelector('#password'); "
            r"return [(u && u.value ? u.value.length : 0), (p && p.value ? p.value.length : 0)]; })()",
        )
        if isinstance(r, list) and len(r) == 2:
            return int(r[0]), int(r[1])
    except Exception:
        pass
    return -1, -1


def _login_evidence(session: str) -> str:
    """登录失败现场的 DOM 证据快照（面板/验证码区/表单/URL）。"""
    try:
        v = oc.eval_js(
            session,
            r"(() => { const code = document.querySelector('#code_area'); "
            r"const lp = document.querySelector('#login_panel'); "
            r"return JSON.stringify({code:(code?getComputedStyle(code).display:'-'), "
            r"panel:(lp?getComputedStyle(lp).display:'-'), "
            r"form:!!document.querySelector('#logon_button'), url:location.href}); })()",
        )
        return str(v)[:220]
    except Exception:
        return "?"


def login(session: str, creds: Optional[dict] = None, window: Optional[str] = None,
          force_logout: bool = True, log: Optional[Callable[[str], None]] = None,
          prog: Optional[Progress] = None) -> str:
    """执行一轮登录。返回登录方式：'login_click'（点了登录）或 'sso_auto'（SSO 直接放行）。

    竞态说明（实测发现）：oauth.jsp 自动跳 ssoLogin.do 时地址栏会短暂出现
    elective.pku.edu.cn，随后可能回弹到登录页；且登录表单可能在"填好后、点登录前"
    被页面重定向走（#logon_button 消失 → selector_not_found）。因此：
    - 只有确认落在「有菜单/补退选」的工作页才算成功；
    - 点登录前校验表单在位，不在位就等待/整轮重试；
    - 整轮最多尝试 2 次；失败时把 页面URL/表单状态/页面提示 带进异常（会写入日志）。
    """
    login_errs: list[str] = []
    for attempt in (1, 2):
        if force_logout and attempt == 1:
            # 先登出，确保这一轮真的重新登录（重试时不重复登出，减少流量）
            try:
                oc.open(session, LOGOUT_URL, window=window)
                sleep_rand(0.8, 1.6)
            except Exception:
                pass
            if prog:
                prog.step("登出旧会话")
        oc.open(session, LOGIN_URL, window=window)
        sleep_rand(1.5, 3.5)  # 给密码管理器自动填充留时间

        url = oc.get_url(session)
        if "elective.pku.edu.cn" in url:
            # 可能是中转页：等几秒确认没有回弹回登录页
            sleep_rand(2.0, 3.0)
            if _on_workable_page(session):
                if prog:
                    prog.step("已通过会话直接进入选课系统")
                return "sso_auto"
            # 回弹到了登录表单，落到下面正常登录流程

        # 等登录表单就位（重定向进行中 #logon_button 可能短暂不存在）
        deadline = time.time() + 12.0
        while time.time() < deadline and not _form_present(session):
            sleep_rand(0.8, 1.5)
        if not _form_present(session):
            login_errs.append(f"第{attempt}次：登录页未就位（无#logon_button），url={oc.get_url(session)}")
            continue

        # 确保「账号登录」面板可见：切面板的 JS 可能重渲染表单，
        # 因此【先切面板、再填值】，避免清空已填内容；隐藏时点登录无效。
        if not _login_panel_visible(session):
            for _ in range(2):
                try:
                    oc.click(session, "#login_panel_top_bar")
                    sleep_rand(1.0, 1.8)
                except Exception:
                    pass
                if _login_panel_visible(session):
                    break
            if not _login_panel_visible(session):
                login_errs.append(f"第{attempt}次：无法显示「账号登录」面板（{_login_evidence(session)}）")

        # 聚焦用户名框并确认焦点（触发密码管理器填充）；失败不阻塞，继续填值
        if not _focus_user(session):
            login_errs.append(f"第{attempt}次：用户名框焦点未确认")

        # 填凭据
        if creds and creds.get("username"):
            oc.fill(session, "input#user_name", creds["username"])
            sleep_rand(0.6, 1.2)
        if creds and creds.get("password"):
            oc.fill(session, "input#password", creds["password"])
            sleep_rand(0.6, 1.2)
        else:
            sleep_rand(0.8, 1.5)  # 未配置密码：等自动填充落盘（人类约1秒）

        if not _form_present(session):
            login_errs.append(f"第{attempt}次：填写后页面被重定向走（无#logon_button）")
            continue

        # 校验确实填上了；没填上就再补填一次
        u_len, p_len = _filled_len(session)
        if u_len <= 0 or p_len <= 0:
            login_errs.append(f"第{attempt}次：字段未填上（user={u_len}, pass={p_len}），已补填")
            try:
                if creds and creds.get("username"):
                    oc.fill(session, "input#user_name", creds["username"])
                if creds and creds.get("password"):
                    oc.fill(session, "input#password", creds["password"])
                sleep_rand(1.0, 1.8)
            except Exception:
                pass
            u_len, p_len = _filled_len(session)

        # 提交登录：先聚焦密码框回车（原生表单提交，实测稳定），
        # 未跳转再点登录按钮兜底
        try:
            oc.click(session, "#password")
            sleep_rand(0.6, 1.0)
        except Exception:
            pass
        try:
            oc.keys(session, "Enter")
        except Exception:
            pass
        if _poll_landed(session, timeout_s=20.0):
            return "login_click"
        if prog:
            prog.step("填写账号并点击登录")
        clicked = _click_logon(session)
        if _poll_landed(session, timeout_s=20.0):
            return "login_click"
        login_errs.append(
            f"第{attempt}次：提交后未进入选课页（filled=u{u_len}/p{p_len}，"
            f"现场={_login_evidence(session)}）"
            + ("，点登录时表单已被重定向走" if not clicked else ""))
        sleep_rand(3.0, 5.0)

    # 失败现场收集（会经 watcher 写入 data/courser.log）
    try:
        url_now = oc.get_url(session)
        form_ok = _form_present(session)
        page = oc.eval_js(session, "(() => (document.body.innerText || '').slice(0, 300))()")
        console = oc.console(session)[-500:]
    except Exception:
        url_now, form_ok, page, console = "?", False, "", ""
    detail = "；".join(login_errs) or "未知"
    raise LoginError(
        f"登录未成功。尝试记录：{detail}；当前 url={url_now}，登录表单在位={form_ok}；"
        f"页面提示：{page}\n浏览器控制台：{console}\n"
        f"提示：若页面要求验证码/二次验证（courser 不输入验证码），请先在 Chrome 手动登录一次；"
        f"若是自动填充未生效，请在「设置」配置学号/密码。")


# ---------------------------------------------------------------------------
# 进入补退选
# ---------------------------------------------------------------------------

def _find_supplement_tab(session: str) -> Optional[str]:
    """在会话标签页里找已打开补退选页的标签（点击可能开新标签）。"""
    try:
        for t in oc.tab_list(session):
            u = t.get("url") or ""
            if "SupplyCancel" in u or "supplement" in u:
                return t.get("page") or ""
    except Exception:
        pass
    return None


def goto_supplement(session: str, window: Optional[str] = None,
                    log: Optional[Callable[[str], None]] = None,
                    prog: Optional[Progress] = None) -> str:
    url = oc.get_url(session)
    if "SupplyCancel" in url or "supplement" in url:
        return url
    # 点左侧菜单（#menu）里的「补退选」入口，避免点到选课时间表里的同名链接
    for attempt in (1, 2):
        try:
            env = oc.click(session, '#menu a[href*="SupplyCancel.do"]')
            clicked = bool(env.get("clicked")) and int(env.get("matches_n") or 1) >= 1
        except Exception:
            clicked = False
        if not clicked:
            try:
                env = oc.click(session, 'a[href*="SupplyCancel.do"]', nth=0)
                clicked = bool(env.get("clicked")) and int(env.get("matches_n") or 1) >= 1
            except Exception:
                clicked = False
        if not clicked:
            sleep_rand(2.0, 3.5)
            continue
        if log:
            log(f"已点击「补退选」（第 {attempt} 次）")
        # 确认页面发生预期变化：当前页或新标签进入补退选页；否则重试点击
        deadline = time.time() + (16.0 if attempt == 1 else 20.0)
        while time.time() < deadline:
            sleep_rand(0.8, 1.5)
            cur = oc.get_url(session)
            if "SupplyCancel" in cur or "supplement" in cur:
                if prog:
                    prog.step("已进入补退选页")
                sleep_rand(1.0, 2.0)
                return cur
            page = _find_supplement_tab(session)
            if page:
                try:
                    oc.tab_select(session, page)
                    sleep_rand(1.0, 2.0)
                    return oc.get_url(session)
                except Exception:
                    pass
        if attempt == 1:
            sleep_rand(1.5, 3.0)
    raise FetchError(f"点击「补退选」后页面未变化，当前 url={oc.get_url(session)}")


# ---------------------------------------------------------------------------
# 动态翻页抓取（仅可用列表）
# ---------------------------------------------------------------------------

def walk_pages(session: str, window: Optional[str] = None,
               pacing: tuple[float, float] = (0.8, 2.0),
               max_pages: int = 100,
               log: Optional[Callable[[str], None]] = None,
               prog: Optional[Progress] = None) -> tuple[list[Course], dict]:
    courses: list[Course] = []
    pages = 0
    warning_hit = False
    prev_signature: Optional[str] = None
    meta = {"pages": 0, "finished": False, "warning_hit": False}

    while pages < max_pages:
        # 先等课程表渲染出来再提取（页面未加载完就抓会拿到空数据）
        first = pages == 0
        if not _wait_table(session, timeout_s=25.0 if first else 15.0, log=log):
            ok_wait = False
            for _ in range(2):  # 重试等待两轮
                sleep_rand(2.0, 3.5)
                if _table_ready(session):
                    ok_wait = True
                    break
            if not ok_wait:
                if log:
                    log("课程表长时间未出现（页面未加载/被拦截），本轮提前结束")
                meta["finished"] = True
                break

        data = oc.eval_js(session, _EXTRACT_JS)
        if not isinstance(data, dict):
            raise FetchError("页面提取失败：eval 未返回 JSON")
        page_courses, pager, warned = _parse_page(data)
        # 有表但解析出 0 门：可能仍在渲染/网络慢，重取一次
        if not page_courses and data.get("tables"):
            sleep_rand(1.5, 2.5)
            data = oc.eval_js(session, _EXTRACT_JS)
            if isinstance(data, dict):
                page_courses, pager, warned = _parse_page(data)
        if prog is not None:
            tot = (pager.get("total") if pager.get("total") else None) or None
            if prog.total is None and tot:
                prog.total = prog.done + tot
            n = pages + 1
            denom = tot or prog.total
            prog.step(f"正在读取课程列表 第 {n}/{denom} 页" if denom else f"正在读取第 {n} 页")
        # 记录页码：邮件按选课网顺序（页号升序、同页从上到下）发送
        for c in page_courses:
            c.page = pages + 1
        courses.extend(page_courses)
        warning_hit = warning_hit or warned
        pages += 1

        # 同页重复护栏：翻页点击失败时可能停在原页，连续两页内容完全一样就停
        if pages >= 2 and page_courses and prev_signature == _sig(page_courses):
            if log:
                log("检测到重复页（翻页未生效），本轮提前结束")
            meta["finished"] = True
            break
        prev_signature = _sig(page_courses) if page_courses else prev_signature

        if log:
            log(f"第 {pages} 页：{len(page_courses)} 门课（累计 {len(courses)}）")

        if not pager["has_next"]:
            meta["finished"] = True
            break
        cur, total = pager.get("cur"), pager.get("total")
        if cur is not None and total is not None and cur >= total:
            meta["finished"] = True
            break
        nxt = pager.get("next_href")
        if not nxt:
            meta["finished"] = True
            break

        # 用「点击 Next」翻页——绝不直接改 URL 跳页（易触发风控）。
        # 点击失败：再点一次作为重试；仍失败则本轮到这里为止（少抓几页，
        # 保持"一切操作都是点击"），由下一轮继续。
        sleep_rand(*pacing)
        clicked = oc.click_by(session, role="link", name="Next")
        if not clicked:
            sleep_rand(0.8, 1.5)
            clicked = oc.click_by(session, role="link", name="Next")
        if not clicked:
            if log:
                log("翻页点击失败，本轮提前结束（不直接跳 URL）")
            meta["finished"] = True
            break
        sleep_rand(0.8, 1.8)

    meta["pages"] = pages
    meta["warning_hit"] = warning_hit
    return courses, meta


def fetch_round(session: str, creds: Optional[dict] = None, window: Optional[str] = None,
                pacing: tuple[float, float] = (0.8, 2.0),
                force_logout: bool = True,
                log: Optional[Callable[[str], None]] = None,
                on_progress: Optional[Callable[[int, Optional[int], str], None]] = None) -> FetchResult:
    """完整一轮：登录 → 补退选 → 翻页抓取。

    on_progress(done, total, op)：实时报告抓取进度与当前操作（total 未知前为 None）。
    """
    result = FetchResult()
    prog = Progress(on_progress) if on_progress else None
    try:
        result.login_mode = login(session, creds=creds, window=window,
                                  force_logout=force_logout, log=log, prog=prog)
        goto_supplement(session, window=window, log=log, prog=prog)
        result.courses, meta = walk_pages(session, window=window, pacing=pacing,
                                          log=log, prog=prog)
        result.pages = meta.get("pages", 0)
        result.warning_hit = bool(meta.get("warning_hit"))
    except (FetchError, oc.OpenCliError) as exc:
        result.ok = False
        result.error = str(exc)
    return result