<div align="center">

# courser

### PKU 补退选空余名额监控 TUI

[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](https://github.com/xjsongphy/courser)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.12-blue.svg)](https://www.python.org/)
[![Built with Textual](https://img.shields.io/badge/built%20with-Textual-green.svg)](https://github.com/Textualize/textual)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

基于 **opencli + gws** 的北大补退选空余名额监控工具。以人类节奏定期登录选课系统，解析 **限数/已选**；命中筛选（课程名 / 课程类别 / 开课院系）且有空余名额时，经 gws 发送提醒邮件。

> 仅供监控本人账号，节奏远低于人工正常操作频率，请遵守学校规定。

## 功能特性

- **三维度多值筛选** — 课程名 / 课程类别 / 开课院系，多选并存，「任一 / 全部」命中可切换
- **名额邮件提醒** — 限数 > 已选 → gws 发邮件（选课网表格样式），同课冷却去重
- **每小时发送上限** — 1 小时内最多发 n 封（默认 5，设置可调）；只限发信、不影响查询轮次
- **每轮重新登录** — 登出 → IAAA 登录 → 补退选，不长期挂会话
- **动态页数解析** — 每轮读取分页器真实翻页，页数变化无需改配置
- **人类节奏** — 操作随机间隔、轮询 ± 抖动；不输验证码，风控自动降速提示
- **风控触发率显示** — 状态栏实时评估「请勿使用刷课机」警告的触发率（请求频率 + 页面警告语检测）；检测到警告语判 100% 并自动放慢节奏
- **持久监控 TUI** — 主页 = 状态摘要 + 最近抓取课程 + 最近一条事件；全中文，配色克制（默认正文 / bold 标题 / dim 次要 / cyan 交互与当前值 / green 成功 / yellow 警告 / red 失败），无主页输入框，纯键盘：
  `空格 开始/停止 · r 立即抓取 · 1/2/3 视图(全部/符合筛选/只看空余) · ↑↓ 浏览 · 回车 详情 · f 筛选 · s 设置 · l 日志 · h 帮助 · q 退出`
- **课程信息完整** — 主页课程列表沿用「最近一次成功结果」的字段（页/课程号/课程名/课程类别/开课单位/教师/限选/空余/状态），按终端宽度自动增减列、只截断超长名称
- **筛选 pi 式 selector** — 独立「筛选」页：顶部输入即可过滤（支持中文；↑↓ 选择、空格 选中/取消、有匹配回车选中、无匹配回车即作为自定义条目加入；Tab 切维度）；空输入回车保存、Esc 放弃（未保存不落盘）。可选候选取自最近一次抓取，无结果时进入本页会自动先抓一轮
- **设置 draft 事务** — 设置改动先进草稿：ctrl+s 保存、Esc 放弃、t 测试邮件（只用当前改动不落盘）
- **日志移出主页** — 完整运行日志放独立「日志」页（l），主页只留最近一条事件；落盘 data/courser.log 不变
- **首启就绪向导** — 首次启动整页引导，填写收件邮箱后「就绪→回车开始」，取代“按某个数=我已完成”；Esc 永不保存
- **后台浏览器** — opencli 驱动真实 Chrome，弹出不抢焦点，可点开查看实时进度

## 工作原理

```
TUI → Watcher 线程，每轮：
  1. 登出旧会话（logout.do + iaaa logout.jsp）
  2. 打开 IAAA 登录页
     · 已配置学号/密码 → 填充后点登录
     · 未配置 → 等待自动填充，直接点登录
     · 遇验证码 → 放弃本轮并提示
  3. 进入「补退选」，按分页器动态翻页，只读提取 课程/类别/开课单位/限数/已选
  4. 筛选命中且 限数 > 已选 → 冷却去重后经 gws 发邮件
```

## 快速开始

**前置依赖**（自行安装配置两个官方 CLI）：

| 工具 | 用途 | 官方地址 | 配置 |
|------|------|----------|------|
| **opencli** | 驱动浏览器 | <https://github.com/jackwener/OpenCLI> | `npm i -g @jackwener/opencli` + Chrome 扩展 + daemon；`opencli doctor` 全绿 |
| **gws** | 发送邮件 | <https://github.com/googleworkspace/cli> | `brew install gws` 或 `npm i -g @googleworkspace/cli`；`gws auth login` |

其他：Python ≥ 3.12、[uv](https://docs.astral.sh/uv/)、Chrome 保存北大学号密码（或 TUI「设置」中配置）。

```bash
git clone https://github.com/xjsongphy/courser && cd courser
uv sync
cp config.example.json config.json
cp .env.example .env

uv run courser        # 启动 TUI；首次启动弹配置向导
```

## 配置

`config.json`（gitignored，不入库；TUI「设置」是唯一修改入口）：

> 运行日志：每轮全量记录到 `data/courser.log`（gitignored，自动滚动），
> 登录失败/发送失败/风控等现场都会写进去，排查问题先看它。

```jsonc
{
  "first_run_done": false,          // 首次向导标记
  "interval_min": 8.0,              // 轮询间隔（分钟），±40% 抖动
  "interval_jitter": 0.4,
  "page_delay_min": 6.0,            // 翻页随机间隔（秒）
  "page_delay_max": 14.0,
  "session": "courser-watch",
  "window": "background",           // 后台窗口，弹出不抢焦点
  "force_relogin": true,
  "credentials": { "username": "", "password": "" },   // 留空=依赖自动填充
  "filters": {
    "names": [],
    "categories": ["通识课(通选课III)", "通识课(通识核心课III)"],
    "depts": ["英语语言文学系"],
    "match": "any"                  // any=任一命中 / all=全部命中
  },
  "notify": {
    "to": "you@example.com",        // 收件邮箱
    "gws_from": "",                 // gws 发件账号（可选）
    "min_interval_min": 15.0        // 同课通知冷却（分钟）
    "max_per_hour": 5               // 每小时最多发送封数（只限发信，不影响查询）
  }
}
```

## 常用命令

```bash
uv run courser --once                      # 跑一轮并打印结果（可配 cron）
uv run python scripts/test_mail.py         # 测试邮件
uv run python scripts/test_pipeline.py      # 全链路测试（解析→判断→冷却→预算→发送）
uv run python scripts/test_unit.py          # 单元测试（config/filters/risk/notifier/run_round）
uv run python scripts/simulate_seats.py    # 模拟"有空余"触发提醒邮件
uv run python scripts/smoke_tui.py         # TUI 无头冒烟
```

## 项目结构

```
courser/
├── courser/            # 核心包
│   ├── opencli.py      # opencli 子进程封装
│   ├── logfile.py      # 持久化日志 data/courser.log（自动滚动）
│   ├── human.py        # 人类节奏：随机间隔、抖动
│   ├── fetch.py        # 登录 → 补退选 → 动态翻页只读抓取
│   ├── filters.py      # 三维度多值筛选
│   ├── notifier.py     # gws 邮件通知（去重/冷却在 watcher）
│   ├── watcher.py      # 后台监控线程（每轮重新登录）
│   ├── config.py       # config.json + .env
│   └── tui.py          # 纯文本 TUI（主页 + 筛选/设置/日志/帮助/详情/首启）
├── scripts/            # test_pipeline / test_unit / test_mail / simulate_seats / smoke_tui
├── config.example.json
├── .env.example
└── pyproject.toml      # uv 管理（入口 courser.tui:main）
```

## 架构

| 模块 | 职责 |
|------|------|
| `tui.py` | 持久监控 TUI：主页状态摘要 + 课程列表 + 单事件；筛选/设置/日志/帮助/详情/首启子页；Esc 永不保存 |
| `watcher.py` | 按随机抖动间隔轮询；同课通知冷却去重 |
| `fetch.py` | 登录 / 进入补退选 / `Page X of Y` 动态翻页，只读提取限数与已选 |
| `opencli.py` | opencli CLI 通信层（eval / click / fill / open） |
| `notifier.py` | 组装 RFC822 邮件 → `gws gmail users messages send` |
| `config.py` | config.json + .env 加载 |
| `human.py` | 随机延迟工具 |

## 开发

```bash
uv run python scripts/test_pipeline.py    # 全链路测试：解析→判断→冷却→预算→发送（mock gws）
uv run python scripts/test_unit.py        # 单元测试：config/filters/risk/notifier/run_round
uv run python scripts/smoke_tui.py        # TUI 无头冒烟：界面打开、快捷键、筛选/设置交互
uv run courser --once                     # 端到端一轮（需已配置 opencli / gws / 登录凭据）
```

> 说明：浏览器侧的真实登录→翻页→抓取（`_EXTRACT_JS`/`login`/`walk_pages`）依赖
> 真实的北大选课网会话，无法在沙箱里自动化回归，请用 `uv run courser --once` 做最终端到端确认。
> 其余解析、判断、通知决策、邮件组装与发送（mock gws）均有上述测试覆盖。

## 参考项目

- [OpenCLI](https://github.com/jackwener/OpenCLI) — 浏览器驱动层
- [googleworkspace/cli](https://github.com/googleworkspace/cli) — gws，Gmail API 发送
- [Textual](https://github.com/Textualize/textual) / [rich](https://github.com/Textualize/rich) — TUI 与富文本
- [codex](https://github.com/openai/codex) — TUI 配色 / 纯文本页面交互参考

## License

MIT License，见 [LICENSE](LICENSE)。