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
IAAA_LOGOUT_URL = "https://iaaa.pku.edu.cn/iaaa/logout.jsp"

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
  const tables = [...document.querySelectorAll('table')].map(parseTable).filter(Boolean);
  const body = document.body.innerText || '';
  const pm = body.match(/Page\s+(\d+)\s+of\s+(\d+)/i);
  const next = [...document.querySelectorAll('a')].find(a => norm(a.textContent) === 'Next');
  return JSON.stringify({
    url: location.href,
    pager: pm ? { cur: +pm[1], total: +pm[2] } : null,
    has_next: !!next,
    next_href: next ? next.getAttribute('href') : null,
    tables: tables.map(t => ({ nrows: t.rows.length, header: t.header, rows: t.rows }))
  });
})()
"""


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


def _parse_page(data: dict) -> tuple[list[Course], dict]:
    """返回 (可用课程列表, 分页信息)。只关心第一个含课程表头的表格。"""
    courses: list[Course] = []
    for t in data.get("tables") or []:
        header, rows = t.get("header") or [], t.get("rows") or []
        for r in rows:
            courses.append(_parse_course(header, r.get("cells") or [], r.get("links") or []))
        break  # 只取第一个课程列表表格（补退选可用列表）
    pager = data.get("pager") or {}
    return courses, {"has_next": bool(data.get("has_next")),
                     "next_href": data.get("next_href"),
                     "cur": (pager or {}).get("cur"),
                     "total": (pager or {}).get("total")}


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
        sleep_rand(1.5, 2.5)
        if _on_workable_page(session):
            return True
    return False


def login(session: str, creds: Optional[dict] = None, window: Optional[str] = None,
          force_logout: bool = True, log: Optional[Callable[[str], None]] = None) -> str:
    """执行一轮登录。返回登录方式：'login_click'（点了登录）或 'sso_auto'（SSO 直接放行）。

    注意：oauth.jsp 自动跳转 ssoLogin.do 的瞬间，地址栏会短暂出现 elective.pku.edu.cn，
    但随后可能回弹到登录页。因此必须确认落在「有菜单/补退选」的页面才算成功。
    """
    if force_logout:
        # 先登出，确保下一轮真的重新登录
        for u in (LOGOUT_URL, IAAA_LOGOUT_URL):
            try:
                oc.open(session, u, window=window)
                sleep_rand(0.8, 1.6)
            except Exception:
                pass
    oc.open(session, LOGIN_URL, window=window)
    sleep_rand(1.5, 3.0)  # 给密码管理器自动填充留时间

    url = oc.get_url(session)
    if "elective.pku.edu.cn" in url:
        # 可能是中转页：等几秒确认没有回弹回登录页
        sleep_rand(2.0, 3.0)
        if _on_workable_page(session):
            return "sso_auto"
        # 回弹到了登录表单，落到下面正常登录流程

    # 登录表单：先聚焦用户名框，触发密码管理器的自动填充
    try:
        oc.click(session, "#user_name")
    except Exception:
        pass

    if creds and creds.get("username"):
        oc.fill(session, "input#user_name", creds["username"])
        sleep_rand(0.6, 1.2)
    if creds and creds.get("password"):
        oc.fill(session, "input#password", creds["password"])
        sleep_rand(0.6, 1.2)
    else:
        sleep_rand(1.8, 3.2)  # 未配置密码：多等一会儿，信任密码管理器自动填充

    # 最多两击登录；若第一击未成功（自动填充还没落盘等），稍后再试一次
    for attempt in (1, 2):
        oc.click(session, "input#logon_button")
        if _poll_landed(session, timeout_s=22.0 if attempt == 1 else 18.0):
            return "login_click"
        sleep_rand(3.0, 5.0)

    # 仍在 iaaa 域或错误页：拿不到可跳转的登录态，多半是要验证码/二次验证，
    # 或密码管理器在自动化窗口中没有自动填充（此时请在「设置」里配置学号/密码）
    try:
        page = oc.eval_js(session, "(() => (document.body.innerText || '').slice(0, 300))()")
    except Exception:
        page = ""
    raise LoginError(
        f"登录未成功（可能要求验证码/二次验证，或密码管理器未自动填充）。"
        f"url={oc.get_url(session)} 页面提示：{page}\n"
        f"提示：若是自动填充未生效，请在 courser「设置」中配置 学号/密码，"
        f"或先在 Chrome 中手动登录一次。）")


# ---------------------------------------------------------------------------
# 进入补退选
# ---------------------------------------------------------------------------

def goto_supplement(session: str, window: Optional[str] = None,
                    log: Optional[Callable[[str], None]] = None) -> str:
    url = oc.get_url(session)
    if "SupplyCancel" in url or "supplement" in url:
        return url
    oc.click(session, 'a[href*="SupplyCancel.do"]')
    if not _poll_url(session, "SupplyCancel", timeout_s=25.0, log=log) and \
       not _poll_url(session, "supplement", timeout_s=10.0, log=log):
        raise FetchError(f"无法进入补退选页面，当前 url={oc.get_url(session)}")
    sleep_rand(1.5, 3.0)
    return oc.get_url(session)


# ---------------------------------------------------------------------------
# 动态翻页抓取（仅可用列表）
# ---------------------------------------------------------------------------

def walk_pages(session: str, window: Optional[str] = None,
               pacing: tuple[float, float] = (6.0, 14.0),
               max_pages: int = 100,
               log: Optional[Callable[[str], None]] = None) -> tuple[list[Course], dict]:
    courses: list[Course] = []
    pages = 0
    meta = {"pages": 0, "finished": False}

    while pages < max_pages:
        data = oc.eval_js(session, _EXTRACT_JS)
        if not isinstance(data, dict):
            raise FetchError("页面提取失败：eval 未返回 JSON")
        page_courses, pager = _parse_page(data)
        courses.extend(page_courses)
        pages += 1

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

        # 相邻页面操作之间随机间隔，模仿人类
        sleep_rand(*pacing)
        oc.open(session, _absolute(nxt), window=window)
        sleep_rand(2.0, 4.0)

    meta["pages"] = pages
    return courses, meta


def fetch_round(session: str, creds: Optional[dict] = None, window: Optional[str] = None,
                pacing: tuple[float, float] = (6.0, 14.0),
                force_logout: bool = True,
                log: Optional[Callable[[str], None]] = None) -> FetchResult:
    """完整一轮：登录 → 补退选 → 翻页抓取。"""
    result = FetchResult()
    try:
        result.login_mode = login(session, creds=creds, window=window,
                                  force_logout=force_logout, log=log)
        goto_supplement(session, window=window, log=log)
        result.courses, meta = walk_pages(session, window=window, pacing=pacing, log=log)
        result.pages = meta.get("pages", 0)
    except (FetchError, oc.OpenCliError) as exc:
        result.ok = False
        result.error = str(exc)
    return result