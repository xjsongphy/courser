# courser

**PKU 补退选空余名额监控**：用 opencli 驱动真实 Chrome（后台窗口，不抢焦点），以人类节奏定期登录北大选课系统的补退选页面，解析课程的 **限数/已选**，命中筛选条件（课程名 / 课程类别 / 开课院系，多条目、多维度并存）且有空余名额时，自动向指定邮箱发送提醒邮件。带 Textual TUI。

- 每轮**重新登录**（登出 → IAAA 登录 → 补退选），不长期挂会话
- 相邻操作随机间隔、轮询间隔随机抖动，模仿人类，**绝不输入验证码**
- 登录失败 / 风控 / 需要验证码时自动降速并在 TUI 中提示人工介入，不硬顶
- 通知带**去重与冷却**，避免刷屏
- 分页动态解析（`Page X of Y`），页数变化无需改代码

> ⚠️ 仅用于监控**本人账号**的选课名额变化，节奏远低于人工操作的合理频率，请遵守学校规定。

---

## 目录

- [工作原理](#工作原理)
- [环境要求](#环境要求)
- [安装](#安装)
- [使用 TUI](#使用-tui)
- [配置说明](#配置说明)
- [邮件通知（Gmail 应用专用密码）](#邮件通知gmail-应用专用密码)
- [命令行 / 脚本](#命令行--脚本)
- [项目结构](#项目结构)
- [常见问题](#常见问题)

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
 │  4. 筛选：任一命中 or 全部命中；空余名额=限数-已选>0
 │  5. 命中且有空余 → 带冷却去重后发邮件
 ▼
opencli browser <session> ... (真实 Chrome，后台窗口)
```

调用链完全走 `opencli`（[OpenCLI](https://github.com/jackwener/OpenCLI)），不直接发 HTTP 请求、不绕登录态；所有浏览器操作都是真实页面事件。

> 浏览器会**弹出但留在后台**（`window: background`）：不抢占焦点、不打扰你，
> 窗口真实存在——想实时查看抓取进度时，点开任务栏/Dock 里那个 Chrome 窗口即可。

## 环境要求

courser 需要你自行安装并配置 **两个官方的命令行工具**（本仓库不内置、不替你配置）：

### 1. opencli —— 驱动浏览器（登录、翻页、抓取）

- 官方仓库：<https://github.com/jackwener/OpenCLI>（npm 包 `@jackwener/opencli`）
- 安装：`npm install -g @jackwener/opencli`
- 配置：安装 Chrome 扩展 [OpenCLI](https://chromewebstore.google.com/detail/opencli/ildkmabpimmkaediidaifkhjpohdnifk) 并启动 daemon；检查 `opencli doctor` 全绿

### 2. gws —— 发送提醒邮件（Google Workspace CLI）

- 官方仓库：<https://github.com/googleworkspace/cli>（npm 包 `@googleworkspace/cli`）
- 安装：`brew install gws` 或 `npm i -g @googleworkspace/cli`
- 配置：`gws auth login`（浏览器完成一次 OAuth2 授权）

> 两者都只在本机运行：opencli 控制你自己的 Chrome，gws 使用你自己的 Google 账号发邮件。

### 其他要求

- macOS / Linux（已适配 macOS）
- Python ≥ 3.12 + [uv](https://docs.astral.sh/uv/)（本项目用 uv 管理环境）
- Chrome 中有北大学号/密码的自动填充（**或**在 courser「设置」中直接配置学号/密码，
  二选一；部分环境下自动化窗口不做自动填充，建议直接配置）

## 安装

```bash
git clone git@github.com:<you>/courser.git   # 或直接在本目录
cd courser
uv sync                                     # 创建 .venv 并安装依赖
cp config.example.json config.json          # 初始配置（可选，TUI 里也能改）
cp .env.example .env                        # 敏感信息（可选）
```

验证：`uv run python scripts/smoke_tui.py`（不连浏览器的 TUI 冒烟测试）。

## 使用 TUI

```bash
uv run courser          # 或 uv run python -m courser.tui
```

界面由**菜单栏**驱动（也提供快捷键，详见底部 Footer / 帮助）：

> **首次启动**会弹出「首次配置」向导，要求先配置 gws 与收件邮箱
> （之后仍可在「⚙ 设置」里修改，且未完成配置前无法启动监控）。

| 菜单 | 功能 |
|------|------|
| ⏵ 监控 | 开始/停止定时轮询；「立即抓取」手动跑一轮；「间隔」修改轮询分钟数 |
| 🎯 筛选 | pi 风格筛选管理：**顶部查询输入框即输即滤**，回车添加/切换选中；`1/2/3` 切换 课程名/课程类别/开课院系 三个维度，各维度可多选、可并存；`m` 切换「任一命中/全部命中」；`d` 删除条目；`c` 把输入内容作为自定义条目添加（如 `通识核心课I类`） |
| ⚙ 设置 | **所有配置的唯一入口**：账号凭据、邮件通知、轮询节奏、浏览器会话、筛选组合模式，集中一处 |
| ❓ 帮助 | 使用说明 |

课程表格：★ 表示命中筛选；空余列为绿色表示有空余名额；`v` 切换视图（全部 / 命中筛选 / 有空余名额）。

默认筛选（`config.example.json`）即按你的需求预设：开课院系=英语语言文学系 **或** 课程类别含 通识课I类/通识核心课I类。

## 配置说明

所有配置集中在 `config.json`（已被 .gitignore 忽略，不会入库）；敏感项也可放 `.env`（环境变量优先）：

```jsonc
{
  "first_run_done": false,       // 首次配置向导完成标记（初次启动会强制弹向导）
  "interval_min": 8.0,           // 轮询基本间隔（分钟），实际 ±40% 随机抖动
  "interval_jitter": 0.4,
  "page_delay_min": 6.0,         // 相邻翻页随机间隔（秒）
  "page_delay_max": 14.0,
  "session": "courser-watch",    // opencli 会话名
  "window": "background",        // 浏览器窗口模式：background=后台弹出、不抢焦点
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
    "gws_from": "",                // gws 发件账号（Gmail 地址，可选；默认取认证账号）
    "min_interval_min": 15.0       // 同一课程两次通知的最小间隔（分钟）
  }
}
```

> `config.json` 与 `.env` 均被 .gitignore 忽略，不会提交到仓库。

## 邮件通知（gws = Google Workspace CLI）

courser 通过 **gws**（[googleworkspace/cli](https://github.com/googleworkspace/cli)）命令行工具发送邮件（Gmail API），**不需要** SMTP 密码/应用专用密码：

1. 安装 gws（macOS 已装；其他平台二选一）：

   ```bash
   brew install gws            # 或 npm i -g @googleworkspace/cli
   gws auth login              # 浏览器完成 OAuth2 授权（仅一次）
   ```

2. 在 courser「设置 → 邮件通知」填写：
   - **收件邮箱**（提醒发送到的地址，例如 you@example.com）
   - **gws 发件账号**（你的 Gmail 地址，可选；默认使用 gws 认证账号）

3. 点 **📧 发送测试邮件** 验证；也可 `uv run python scripts/test_mail.py`

> 未安装/未授权 gws 时，「设置」里会显示 gws ✗；监控不会假报"已发送"。
> gws 也支持 Google Workspace 的 Drive/Sheets 等，courser 只用它的 Gmail 发送能力。

## 命令行 / 脚本

```bash
uv run courser                    # 启动 TUI
uv run courser --once             # 不进入 TUI，直接跑一轮并打印结果（可配 cron）
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
└── pyproject.toml     # uv 管理
```

## 常见问题

- **登录失败，提示"密码管理器未自动填充"**：在 Chrome 里确认已保存该站点密码；多数情况下直接在 courser「设置」配置学号/密码最稳。
- **邮件发不出**：确认 `gws auth login` 已授权、「设置 → 邮件通知」里收件邮箱非空，然后点「发送测试邮件」看日志。
- **要求输入验证码**：courser 不会输入验证码——请到真实 Chrome 手动登录一次，之后降低轮询频率。
- **「立即抓取」很久没动静**：每轮包含重新登录 + 翻页的随机人类间隔，几分钟内完成是正常节奏。
- **opencli 连不上**：`opencli doctor` 检查 daemon / 扩展 / Chrome。

## 免责声明

本项目仅为**个人账号**的低频名额查看提醒，坚持人类节奏、不抢课、不绕过验证码、不做并发轰炸。请遵守北京大学选课系统相关规定及学校纪律，因使用本工具产生的一切后果由使用者自行承担。