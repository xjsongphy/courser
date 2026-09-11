"""注入浏览器的只读提取 JS（原始字符串）。

独立成模块，避免把大块 JS 埋进 3000 行的 Python workflow 文件里。二者均为只读：
只 `querySelectorAll` 收集数据，不点击、不改 DOM，返回 JSON 字符串。
"""

# 提取补退选页主流程 JS：返回可用课程表 + 分页器 + 是否命中风控提示。
EXTRACT_JS = r"""
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

# 统一页面证据。这里只读取 DOM，不在 JS 中决定 workflow；页面优先级和状态转移
# 统一由 client.detect_page() 决定，避免各个 workflow 各自以 URL/局部 selector 猜状态。
PAGE_STATE_JS = r"""
(() => {
  const norm = s => (s == null ? '' : String(s)).replace(/\s+/g, ' ').trim();
  const visible = el => {
    if (!el) return false;
    const cs = getComputedStyle(el), r = el.getBoundingClientRect();
    return cs.display !== 'none' && cs.visibility !== 'hidden' && r.width > 0 && r.height > 0;
  };
  const tb = [...document.querySelectorAll('table')];
  let courseTable = false;
  for (const t of tb) {
    const cs = [...t.querySelectorAll('th,td')].map(c => norm(c.textContent));
    if (cs.some(x => x.includes('课程号')) && cs.some(x => x.includes('限数')))
      { courseTable = true; break; }
  }
  const body = document.body.innerText || '';
  const sessionExpired = /(您尚未登录(?:或者|或)?会话超时|尚未登录[^。]{0,20}会话超时)/.test(body);
  const riskWarning = !courseTable && /(刷课机|过于频繁|频率过高|操作频繁|风控|异常访问|请勿使用)/.test(body);
  const pager = body.match(/Page\s+(\d+)\s+of\s+(\d+)/i);
  return {
    url: location.href,
    ready_state: document.readyState,
    has_login_form: visible(document.querySelector('#logon_button')),
    has_elective_menu: !!document.querySelector('#menu a[href*="SupplyCancel.do"], a[href*="SupplyCancel.do"]'),
    has_course_table: courseTable,
    session_expired: sessionExpired,
    risk_warning: riskWarning,
    has_captcha: visible(document.querySelector('#code_area')),
    page: pager ? +pager[1] : null,
    total_pages: pager ? +pager[2] : null,
    // 兼容旧调用方/测试；新代码应读取上面的 evidence 字段。
    ready: courseTable,
    warning: riskWarning,
  };
})()
"""
