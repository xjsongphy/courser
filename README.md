<div align="center">

# courser

### PKU 补退选空余名额监控 TUI

[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)](https://github.com/xjsongphy/courser)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.12-blue.svg)](https://www.python.org/)
[![Built with Textual](https://img.shields.io/badge/built%20with-Textual-green.svg)](https://github.com/Textualize/textual)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

</div>

基于 **opencli + gws** 的北大补退选空余名额监控工具：以人类节奏定期登录选课系统，解析补退选列表的 **限数/已选**，命中筛选条件（课程名 / 课程类别 / 开课院系）且有空余名额时，通过 **gws** 自动发送提醒邮件。带 Textual TUI。

> ⚠️ 仅用于监控**本人账号**的选课名额变化，节奏远低于人工操作的合理频率，请遵守学校规定。

## 功能特性

### 核心能力
- **每轮重新登录** — 登出旧会话 → IAAA 登录 → 补退选，不长期挂会话，随时从新状态开始
- **动态页数解析** — 每次读取 `Page X of Y` 分页器真实翻页，页数变化无需改配置
- **三维度多值筛选** — 课程名 / 课程类别 / 开课院系，每维度可多选、可并存；「任一命中 / 全部命中」可切换
- **名额邮件提醒** — 限数 > 已选 且命中筛选 → gws 发邮件；同课冷却去重，避免刷屏
- **人类节奏** — 相邻操作随机间隔、轮询间隔 ± 抖动；**绝不输入验证码**，风控/失败自动降速提示

### TUI 体验（Textual）
- **菜单驱动** — 监控 / 筛选 / 设置 / 帮助 菜单栏为主要入口，快捷键仅辅助（底部 Footer 提示）
- **pi 风格筛选器** — 顶部查询输入框**即输即滤**，回车添加/切换选中，`1/2/3` 切换维度，`d` 删除条目
- **课程表格** — ★ 命中筛选、绿色空余名额、`v` 切换 全部 / 命中 / 有空余
- **状态一目了然** — 运行状态、下一轮倒计时、上一轮结果、筛选摘要、gws 状态实时展示
- **首次配置向导** — 初次启动强制引导完成 opencli/gws 与收件邮箱配置，之后仍可在「设置」修改

### 邮件与安全
- **gws 发送** — 走 [Google Workspace CLI](https://github.com/googleworkspace/cli) 的 Gmail API，无需 SMTP 密码
- **后台浏览器** — opencli 控制真实 Chrome：弹出但留在后台、不抢焦点，可随时点开窗口查看实时进度
- **优雅降级** — 未授权/未安装 gws、登录失败、要求验证码时，日志明确提示且不硬顶

## 工作原理

```
TUI (Textual)
 │ 定时/手动触发一轮
 ▼
Watcher 线程
 │  1. logout.do + iaaa logout.jsp（每轮强制重新登录）
 │  2. 打开 IAAA OAuth 登录页
 │       · 配置了 学号/密码 → 填好后点登录
 │       · 未配置 → 等待密码管理器自动填充，一小会后直接点「登录」
 │       · 出现验证码/错误 → 放弃本轮并提示（不输入验证码）
 │  3. 点击「补退选」，解析分页器动态翻页，逐页只读提取
 │     课程号/课程名/课程类别/开课单位/限数/已选/选课状态...
 │  4. 筛选：任一命中 or 全部命中；空余名额 = 限数 - 已选 > 0
 │  5. 命中且有空余 → 带冷却去重后发邮件（gws）
 ▼
opencli browser <session> ... (真实 Chrome，后台窗口)
```

调用链完全走 [opencli](https://github.com/jackwener/OpenCLI)，不直接发 HTTP 请求、不绕登录态；所有浏览器操作都是真实页面事件。

## 快速开始

**前置依赖：** 你需要自行安装并配置 **两个官方命令行工具**（本仓库不内置、不替你配置）：

| 工具 | 用途 | 官方地址 | 配置 |
|------|------|----------|------|
| **opencli** | 驱动浏览器（登录/翻页/抓取） | <https://github.com/jackwener/OpenCLI> | `npm install -g @jackwener/opencli` + Chrome 扩展 + daemon；`opencli doctor` 全绿 |
| **gws** | 发送提醒邮件 | <https://github.com/googleworkspace/cli> | `brew install gws` 或 `npm i -g @googleworkspace/cli`；`gws auth login` |

两者都只在本机运行：opencli 控制你自己的 Chrome，gws 使用你自己的 Google 账号发邮件。

其他要求：Python ≥ 3.12、[uv](https://docs.astral.sh/uv/)、Chrome 中保存北大学号/密码（或直接在 courser「设置」配置）。

### 安装

```bash
git clone https://github.com/xjsongphy/courser && cd courser
uv sync                                    # 创建 .venv 并安装依赖
cp config.example.json config.json         # 初始配置（可选，TUI 里也能改）
cp .env.example .env                       # 敏感信息（可选）
```

### 运行

```bash
uv run courser
```

首次启动会弹出**配置向导**：依次完成 `opencli doctor` / `gws auth login`，然后在「⚙ 设置」填写**学号/密码**（留空则依赖浏览器自动填充）与**收件邮箱**，点「📧 发送测试邮件」验证后即可「⏵ 监控」。

浏览器会**弹出但留在后台**（`window: background`）：不抢占焦点、不打扰你，随时可点开任务栏/Dock 里的 Chrome 窗口查看实时状态。

## 配置

所有配置集中在 `config.json`（已被 .gitignore 忽略，不会入库），TUI「设置」是唯一修改入口；敏感项也可放 `.env`：

```jsonc
{
  "first_run_done": false,       // 首次配置向导标记（初次启动强制弹向导）
  "interval_min": 8.0,           // 轮询基本间隔（分钟），实际 ±40% 随机抖动
  "interval_jitter": 0.4,
  "page_delay_min": 6.0,         // 相邻翻页随机间隔（秒）
  "page_delay_max": 14.0,
  "session": "courser-watch",    // opencli 会话名
  "window": "background",        // 后台窗口，弹出但不抢焦点
  "force_relogin": true,         // 每轮先登出再重新登录
  "credentials": { "username": "", "password": "" },  // 留空=依赖自动填充
  "filters": {
    "names": [],
    "categories": ["通识课I类", "通识核心课I类"],
    "depts": ["英语语言文学系"],
    "match": "any"               // any=任一维度命中 / all=全部维度命中
  },
  "notify": {
    "to": "you@example.com",  // 收件邮箱（提醒发送到的地址）
    "gws_from": "",                // gws 发件账号（Gmail，可选；默认取认证账号）
    "min_interval_min": 15.0       // 同一课程两次通知的最小间隔（分钟）
  }
}
```

## 常用命令

```bash
uv run courser                     # 启动 TUI
uv run courser --once              # 不进入 TUI，直接跑一轮并打印结果（可配 cron）
uv run python scripts/recon_snapshot.py   # 一次性抓全补退选列表 → data/courses_snapshot.json
uv run python scripts/test_mail.py        # 发测试邮件
uv run python scripts/smoke_tui.py        # TUI 无头冒烟测试
```

## 项目结构

```
courser/
├── courser/
│   ├── opencli.py     # opencli 子进程封装（JSON/纯文本信封解析）
│   ├── human.py       # 人类节奏：随机间隔、抖动
│   ├── fetch.py       # 登录 → 补退选 → 动态翻页只读抓取
│   ├── filters.py     # 三维度多值筛选匹配
│   ├── notifier.py    # gws 邮件通知（去重/冷却在 watcher）
│   ├── watcher.py     # 后台监控线程（每轮重新登录）
│   ├── config.py      # config.json + .env
│   └── tui.py         # Textual TUI（菜单 / 筛选 / 设置 / 帮助）
├── scripts/           # recon_snapshot / test_mail / smoke_tui
├── config.example.json
├── .env.example
└── pyproject.toml     # uv 管理（入口 courser.tui:main）
```

## 架构

```
courser/
├── courser/            # 核心包
│   ├── tui.py          # 入口：TUI 应用、菜单、首次向导、设置/筛选界面
│   ├── watcher.py      # 轮询线程：定时触发一轮，通知去重与冷却
│   ├── fetch.py        # 每轮流程：登出 → 登录 → 补退选 → 翻页抓取
│   ├── opencli.py      # 与 opencli CLI 的通信层（eval/click/fill/open）
│   ├── notifier.py     # gws Gmail API 发信（RFC822 → base64url）
│   ├── filters.py      # 任一/全部命中组合匹配
│   ├── config.py       # config.json + .env 加载
│   └── human.py        # 随机延迟工具
├── scripts/            # 辅助脚本（识别快照、测试邮件、无头冒烟）
└── data/               # 运行时数据（git 忽略）：快照、通知状态
```

### 模块职责

| 模块 | 职责 |
|------|------|
| `tui.py` | 菜单栏驱动的 Textual 界面；筛选器（pi 风格）、单一「设置」入口、首次向导 |
| `watcher.py` | 后台线程按随机抖动间隔轮询；同课通知冷却去重 |
| `fetch.py` | 登录/进入补退选/`Page X of Y` 动态翻页，只读提取限数与已选 |
| `notifier.py` | 组装邮件 → `gws gmail users messages send`，未授权优雅提示 |
| `opencli.py` | opencli 子进程封装，JSON 信封解析，超时与错误归一 |

## 开发

```bash
# TUI 无头冒烟（不连浏览器）
uv run python scripts/smoke_tui.py

# 端到端一轮（需已配置好 opencli/gws）
uv run courser --once
```

## 参考项目

- [OpenCLI](https://github.com/jackwener/OpenCLI) — 浏览器驱动层，把网站变成 CLI
- [googleworkspace/cli](https://github.com/googleworkspace/cli) — gws 官方 CLI，Gmail API 发送
- [Textualize/textual](https://github.com/Textualize/textual) — TUI 框架（CSS 样式、DataTable、ListView）
- [Textualize/rich](https://github.com/Textualize/rich) — 终端富文本渲染
- [pi](https://github.com/earendil-works/pi) — 筛选器交互参考（provider/model 即输即滤选择器）

## License

MIT License，详见 [LICENSE](LICENSE)。