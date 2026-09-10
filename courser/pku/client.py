"""浏览器工作流：登录 → 进入补退选 → 动态翻页抓取。

这一层是「workflow」，不关心网页 JSON 如何变成 Course（那是 parser 的事）、
也不负责筛选/通知（那是 runner 的事）。它只依赖：
    opencli 适配器（真实浏览器交互）、parser（纯转换）、extract（注入 JS）、human（节奏）。

流程（每一轮监控都会执行）：
1. 默认先检查当前浏览器会话；有效则直接复用
2. 会话失效时打开 IAAA OAuth 登录页；填凭据或等密码管理器自动填充，点登录
   - 开启 force_relogin 时，才会先退出旧会话再登录
   - 不做验证码输入；出现验证码/错误 → 抛 LoginError，由上层降速暂停
3. 点击菜单「补退选」进入补退选页
4. 动态翻页（每次解析 "Page X of Y" 分页器，不固定页数），逐页只读提取可用课程
"""

from __future__ import annotations

import json
import time
from typing import Callable, Optional

from .. import opencli as oc
from ..human import sleep_rand
from ..models import Course, FetchError, FetchResult, LoginError
from .extract import EXTRACT_JS, PAGE_STATE_JS
from . import parser

LOGIN_URL = (
    "https://iaaa.pku.edu.cn/iaaa/oauth.jsp?appID=syllabus"
    "&appName=%E5%AD%A6%E7%94%9F%E9%80%89%E8%AF%BE%E7%B3%BB%E7%BB%9F"
    "&redirectUrl=http://elective.pku.edu.cn:80/elective2008/ssoLogin.do"
)
ELECTIVE_BASE = "https://elective.pku.edu.cn"
LOGOUT_URL = ELECTIVE_BASE + "/elective2008/logout.do"
# 注意：iaaa.pku.edu.cn/iaaa/logout.jsp 不存在（返回 404），不要用它；
# elective 的 logout.do 已足够（会登出并重定向到 IAAA 登录页）。


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
# 页面在位检测 / 等待（三态）
# ---------------------------------------------------------------------------

PAGE_TABLE = "table"      # 课程表已就绪，可以正常提取
PAGE_WARNING = "warning"  # 明确命中风控阻断页（课程表消失 + 警告文案）
PAGE_TIMEOUT = "timeout"  # 既无课程表也无明确警告：无法确定状态


def _page_state(session: str) -> tuple[bool, bool]:
    """返回 (课程表就绪, 明确风控警告)。eval 异常按未就绪处理。"""
    try:
        r = oc.eval_js(session, PAGE_STATE_JS)
        if isinstance(r, dict):
            return bool(r.get("ready")), bool(r.get("warning"))
    except Exception:
        pass
    return False, False


def _wait_page_state(session: str, timeout_s: float = 20.0,
                     log: Optional[Callable[[str], None]] = None) -> str:
    """等页面进入一种确定状态：课程表就绪 / 明确风控警告 / 超时。

    返回 PAGE_TABLE / PAGE_WARNING / PAGE_TIMEOUT。
    关键：既等课程表出现，也等**阻断性风控警告**出现。风控页会把课程表替换掉，
    若只等课程表会一直等到超时而被误判成"0 页正常完成"（漏报甚至反向记 false）。
    超时（既无课程表也无明确警告）视为“不知道发生了什么”，不计入风控样本。
    """
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        ready, warned = _page_state(session)
        if ready:
            return PAGE_TABLE
        if warned:
            return PAGE_WARNING
        sleep_rand(0.5, 1.0)
    return PAGE_TIMEOUT


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


def _login_error_text(session: str) -> str:
    """常见的登录错误/提示文案（用于区分「账号被拒」与「提交未触发」）。"""
    try:
        v = oc.eval_js(
            session,
            r"(() => { const sels = ['#loginError','.login_error','#errormsg',"
            r"'.error-msg']; "
            r"for (const s of sels) { const el = document.querySelector(s); "
            r"if (el && el.textContent.trim()) return el.textContent.trim().slice(0,120); } "
            r"return ''; })()",
        )
        return str(v or "").strip()
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------

def ensure_login(session: str, creds: Optional[dict] = None,
                 window: Optional[str] = None, force_relogin: bool = False,
                 log: Optional[Callable[[str], None]] = None,
                 prog: Optional[Progress] = None) -> str:
    """确保可用登录态：默认复用当前会话，失效时才走登录流程。

    ``force_relogin`` 是显式的例外：用于账号切换或排障时，每轮先登出再登录。
    """
    if not force_relogin:
        if prog:
            prog.step("检查现有登录状态…")
        if _on_workable_page(session):
            if log:
                log("已检测到有效 PKU 登录状态，复用当前会话")
            if prog:
                prog.step("已复用现有登录会话")
            return "reuse_session"
        if log:
            log("未检测到有效登录状态，进入登录流程")
    return login(session, creds=creds, window=window,
                 force_logout=force_relogin, log=log, prog=prog)

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
    """登录表单可交互：登录按钮 + 用户名框均可见可点（而非隐藏/0 尺寸）。

    不依赖 #login_panel 的高度（页面加载/切面板瞬间 height 可为 0，造成误判
    「无法显示面板」），直接看真正要点/要填的控件是否在位可见。
    """
    try:
        v = oc.eval_js(
            session,
            r"(() => { const visible = el => { if (!el) return false; "
            r"const cs = getComputedStyle(el); const r = el.getBoundingClientRect(); "
            r"return cs.display !== 'none' && cs.visibility !== 'hidden' "
            r"&& r.width > 0 && r.height > 0; }; "
            r"return visible(document.querySelector('#logon_button')) "
            r"&& visible(document.querySelector('#user_name')); })()",
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


def _fill_value(session: str, selector: str, value: str) -> None:
    """受控写入：原生 setter 设值并派发 input/change/blur，登录框架才认。

    opencli 的 fill 只改 DOM.value 不触发事件；IAAA 登录表单（#user_name oninput、
    <form> onsubmit）监听事件读值，直接 fill 提交读到空值 → 停在登录页（实测）。
    统一走 _set_input（原生 setter + 事件，React/受控通用），失败再退回 fill。
    """
    if not _set_input(session, selector, value):
        oc.fill(session, selector, value)


def _set_input(session: str, selector: str, value: str) -> bool:
    """受控写入：用 HTMLInputElement 原生 setter 设值并派发 input/change/blur。

    关键：登录表单是受控输入（#user_name 带 oninput、<form> 带 onsubmit）。
    opencli 的 fill 只改 DOM.value 不触发事件 -> 框架内部状态没拿到值，
    提交时校验认为空 -> 停在登录页、无错误文案（实测，日志 filled 有值却未跳转）。
    这里用原生 setter + 派发事件，等于真实键入，对 React/受控 oninput 都生效。
    """
    js = (
        f"((sel, v) => {{ const el = document.querySelector(sel); if (!el) return false; "
        f"const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set; "
        f"setter.call(el, v); "
        f"for (const t of ['input', 'change', 'blur']) "
        f"el.dispatchEvent(new Event(t, {{ bubbles: true }})); "
        f"return el.value === v; }})({json.dumps(selector)}, {json.dumps(value)})"
    )
    try:
        return oc.eval_js(session, js) is True
    except Exception:
        return False


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
            _fill_value(session, "input#user_name", creds["username"])
            sleep_rand(0.6, 1.2)
        if creds and creds.get("password"):
            _fill_value(session, "input#password", creds["password"])
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
                    _fill_value(session, "input#user_name", creds["username"])
                if creds and creds.get("password"):
                    _fill_value(session, "input#password", creds["password"])
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

    # 失败现场收集（会经 runner 写入 data/courser.log）
    try:
        url_now = oc.get_url(session)
        form_ok = _form_present(session)
        page = oc.eval_js(session, "(() => (document.body.innerText || '').slice(0, 300))()")
        console = oc.console(session)[-500:]
    except Exception:
        url_now, form_ok, page, console = "?", False, "", ""
    login_err_text = _login_error_text(session)
    still_login = ("iaaa.pku.edu.cn" in url_now) or ("oauth.jsp" in url_now)
    detail = "；".join(login_errs) or "未知"
    # 只有真的检测到 #code_area 才提示验证码，避免凭空臆断误导（浏览器可能并无验证码）
    evidence = ""
    try:
        evidence = _login_evidence(session)
    except Exception:
        pass
    has_captcha = bool(evidence) and "code" in evidence and \
        "'code': 'none'" not in evidence and '"code": "none"' not in evidence
    if has_captcha:
        hint = ("页面出现验证码/二次验证（courser 不输入验证码），"
                "请先在 Chrome 手动登录一次（手动登录后可复用会话来继续）。")
    elif login_err_text:
        hint = f"登录页返回错误文案：{login_err_text}（很可能是账号被拒/密码错误）。"
    elif still_login and form_ok:
        hint = ("提交后仍停留在登录页且表单仍在——点登录很可能未触发表单提交"
                "（注意：账号密码虽已写入 DOM，若框架监听 input/change 事件，需要原生键入"
                "而非 fill 直接改 value 才能被识别）。请在 Chrome 手动登录一次后重试。")
    else:
        hint = ("仍未进入选课页（非验证码问题）。请确认：若自动填充未生效，请在「设置」"
                "配置学号/密码；若账号密码已正确填入却仍无法进入，请在 Chrome 手动登录一次后重试。")
    raise LoginError(
        f"登录未成功。尝试记录：{detail}；当前 url={url_now}，登录表单在位={form_ok}，"
        f"是否仍停在登录页={still_login}，登录页错误文案={login_err_text or '无'}；"
        f"页面提示：{page}\n浏览器控制台：{console}\n提示：{hint}")


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


_PAGER_PAGE_JS = (
    r"(() => { const m = (document.body.innerText || '').match("
    r"/Page\s+(\d+)\s+of\s+(\d+)/i); return m ? +m[1] : null; })()"
)


def _supplement_page_number(session: str) -> Optional[int]:
    """返回补退选列表当前页；页面尚未渲染分页器时返回 ``None``。"""
    try:
        value = oc.eval_js(session, _PAGER_PAGE_JS)
    except Exception:
        return None
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def reset_supplement_to_first_page(session: str,
                                   log: Optional[Callable[[str], None]] = None,
                                   prog: Optional[Progress] = None) -> None:
    """复用会话时，将上轮停留的补退选分页复位到第一页。

    不能仅靠 ``goto_supplement``：若当前已经是 SupplyCancel 页面，它会正确地
    原地返回，但页面可能正停在最后一页。这里用分页器自身的 First/1 链接复位，
    不拼接 URL，也不绕过网站正常的翻页流程。
    """
    current = _supplement_page_number(session)
    if current is None or current <= 1:
        return
    if log:
        log(f"检测到补退选列表停在第 {current} 页，正在返回第 1 页")
    if prog:
        prog.step("补退选列表返回第 1 页")

    # 北大旧选课页的分页器使用英文 First；部分皮肤只提供数字页码，因此回退
    # 到精确名称 "1"。两者均由 opencli 的可访问性定位点击，不直接构造分页 URL。
    for name in ("First", "1"):
        try:
            clicked = oc.click_by(session, role="link", name=name)
        except Exception:
            clicked = False
        if not clicked:
            continue
        for _ in range(8):
            sleep_rand(0.4, 0.8)
            page = _supplement_page_number(session)
            if page == 1:
                if log:
                    log("补退选列表已回到第 1 页")
                return

    # 不允许把最后一页当成“只有一页”后静默成功，否则监控结果会不完整。
    page = _supplement_page_number(session)
    raise FetchError(f"补退选列表无法回到第 1 页（当前第 {page or '?'} 页）")


def prepare_fetch_context(session: str, creds: Optional[dict] = None,
                          window: Optional[str] = None,
                          force_relogin: bool = False,
                          log: Optional[Callable[[str], None]] = None,
                          prog: Optional[Progress] = None) -> str:
    """准备一轮抓取的工作上下文，并返回本轮登录方式。

    这里集中维护抓取前的不变量：已认证、位于补退选页面、分页从第 1 页开始。
    ``walk_pages`` 因此只负责读取和翻页，不再隐含假设浏览器当前停留位置。
    """
    login_mode = ensure_login(
        session, creds=creds, window=window,
        force_relogin=force_relogin, log=log, prog=prog)
    if log:
        mode_label = "复用已有会话" if login_mode == "reuse_session" else "重新登录"
        log(f"抓取准备：登录状态={mode_label}")

    supplement_url = goto_supplement(
        session, window=window, log=log, prog=prog)
    if log:
        log(f"抓取准备：补退选页面={supplement_url}")

    reset_supplement_to_first_page(session, log=log, prog=prog)
    if log:
        page = _supplement_page_number(session)
        log(f"抓取准备：当前页={page if page is not None else '未知'}")
    return login_mode


# ---------------------------------------------------------------------------
# 动态翻页抓取（仅可用列表）
# ---------------------------------------------------------------------------

def walk_pages(session: str, window: Optional[str] = None,
               pacing: tuple[float, float] = (0.8, 2.0),
               max_pages: int = 100,
               log: Optional[Callable[[str], None]] = None,
               prog: Optional[Progress] = None,
               obs: Optional[dict] = None) -> tuple[list[Course], dict]:
    courses: list[Course] = []
    pages = 0
    warning_hit = False
    prev_signature: Optional[str] = None
    meta = {"pages": 0, "finished": False, "warning_hit": False}
    # obs 是「观察状态累加器」：即使 walk_pages 中途抛异常，fetch_round 也能从
    # obs 拿回已经看过的页数 / 是否命中警告，不会把"查过几页"误判成"0 页无观察"。
    obs = obs if obs is not None else {}
    obs["pages"] = 0
    obs["warning_hit"] = False

    while pages < max_pages:
        obs["pages"] = pages  # 本页之前已成功读到的页数（异常时也能带出）
        # 先等页面进入确定状态：课程表就绪 或 明确风控警告（不是只等课程表，
        # 否则风控页替换掉课程表时会等到超时而漏掉真正触发的警告）。
        first = pages == 0
        state = _wait_page_state(session, timeout_s=25.0 if first else 15.0, log=log)
        if state == PAGE_WARNING:
            if log:
                log("检测到风控阻断页（课程表消失 + 出现警告文案），本轮提前结束")
            warning_hit = True
            obs["warning_hit"] = True
            meta["warning_hit"] = True
            meta["finished"] = True
            break
        if state == PAGE_TIMEOUT:
            # 既没课程表也没明确警告：无法确定是"被拦"还是"没加载完"，不计入风控样本
            if log:
                log("页面长时间未就绪（无课程表、也无明确警告），本轮提前结束")
            meta["finished"] = True
            break
        # state == PAGE_TABLE：正常课程表

        data = oc.eval_js(session, EXTRACT_JS)
        if not isinstance(data, dict):
            raise FetchError("页面提取失败：eval 未返回 JSON")
        page_courses, pager, warned = parser.parse_page(data)
        # 有表但解析出 0 门：可能仍在渲染/网络慢，重取一次
        if not page_courses and data.get("tables"):
            sleep_rand(1.5, 2.5)
            data = oc.eval_js(session, EXTRACT_JS)
            if isinstance(data, dict):
                page_courses, pager, warned = parser.parse_page(data)
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
        if warned:
            obs["warning_hit"] = True
        pages += 1
        obs["pages"] = pages

        # 同页重复护栏：翻页点击失败时可能停在原页，连续两页内容完全一样就停
        if pages >= 2 and page_courses and prev_signature == parser._sig(page_courses):
            if log:
                log("检测到重复页（翻页未生效），本轮提前结束")
            meta["finished"] = True
            break
        prev_signature = parser._sig(page_courses) if page_courses else prev_signature

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
    """完整一轮抓取：登录 → 补退选 → 翻页抓取。

    on_progress(done, total, op)：实时报告抓取进度与当前操作（total 未知前为 None）。
    """
    result = FetchResult()
    prog = Progress(on_progress) if on_progress else None
    obs: dict = {}
    try:
        result.login_mode = prepare_fetch_context(
            session, creds=creds, window=window,
            force_relogin=force_logout, log=log, prog=prog)
        result.courses, meta = walk_pages(session, window=window, pacing=pacing,
                                          log=log, prog=prog, obs=obs)
        result.pages = meta.get("pages", 0)
        result.warning_hit = bool(meta.get("warning_hit"))
    except oc.OpenCliCancelled as exc:
        result.ok = False
        result.cancelled = True
        result.error = str(exc)
    except (FetchError, oc.OpenCliError) as exc:
        result.ok = False
        result.error = str(exc)
        # walk_pages 中途抛异常也要带回已观察的风控/页数状态，
        # 避免"已检查过几页却被当成 0 页无观察"漏记/误记风控样本。
        result.pages = obs.get("pages", result.pages)
        result.warning_hit = obs.get("warning_hit", result.warning_hit)
    # 是否「有效抓取」：有确定观察结果——真正进入补退选页读到 ≥1 页，
    # OR 明确命中风控阻断警告（警告页 pages 可能为 0，但这恰是最该计入的样本）。
    # 只有这类 attempt 才计入风控触发率分母；登录失败/页面空白超时等无观察的不计入。
    result.warning_checked = result.warning_hit or result.pages >= 1
    return result
