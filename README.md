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
- **名额邮件提醒** — 限数 > 已选 → gws 发邮件，同课冷却去重
- **每轮重新登录** — 登出 → IAAA 登录 → 补退选，不长期挂会话
- **动态页数解析** — 每轮读取分页器真实翻页，页数变化无需改配置
- **人类节奏** — 操作随机间隔、轮询 ± 抖动；不输验证码，风控自动降速提示
- **风控触发率显示** — 状态栏实时评估「请勿使用刷课机」警告的触发率（请求频率 + 页面警告语检测）；检测到警告语判 100% 并自动放慢节奏
- **菜单驱动 TUI** — 监控 / 筛选 / 设置 / 帮助；快捷键辅助
- **pi 风格筛选器** — 顶部输入即输即滤，回车添加 / 切换选中
- **首次配置向导** — 初次启动强制引导 opencli / gws 与收件邮箱，之后仍可在「设置」修改
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
  }
}
```

## 常用命令

```bash
uv run courser --once                      # 跑一轮并打印结果（可配 cron）
uv run python scripts/test_mail.py         # 测试邮件
uv run python scripts/simulate_seats.py    # 模拟"有空余"触发提醒邮件
uv run python scripts/smoke_tui.py         # TUI 无头冒烟
```

## 项目结构

```
courser/
├── courser/            # 核心包
│   ├── opencli.py      # opencli 子进程封装
│   ├── human.py        # 人类节奏：随机间隔、抖动
│   ├── fetch.py        # 登录 → 补退选 → 动态翻页只读抓取
│   ├── filters.py      # 三维度多值筛选
│   ├── notifier.py     # gws 邮件通知（去重/冷却在 watcher）
│   ├── watcher.py      # 后台监控线程（每轮重新登录）
│   ├── config.py       # config.json + .env
│   └── tui.py          # Textual TUI（菜单 / 筛选 / 设置 / 帮助）
├── scripts/            # test_mail / simulate_seats / smoke_tui
├── config.example.json
├── .env.example
└── pyproject.toml      # uv 管理（入口 courser.tui:main）
```

## 架构

| 模块 | 职责 |
|------|------|
| `tui.py` | 菜单驱动 Textual 界面；pi 风格筛选器；单一「设置」入口；首次向导 |
| `watcher.py` | 按随机抖动间隔轮询；同课通知冷却去重 |
| `fetch.py` | 登录 / 进入补退选 / `Page X of Y` 动态翻页，只读提取限数与已选 |
| `opencli.py` | opencli CLI 通信层（eval / click / fill / open） |
| `notifier.py` | 组装 RFC822 邮件 → `gws gmail users messages send` |
| `config.py` | config.json + .env 加载 |
| `human.py` | 随机延迟工具 |

## 开发

```bash
uv run python scripts/smoke_tui.py    # TUI 无头冒烟（不连浏览器）
uv run courser --once                 # 端到端一轮（需已配置 opencli / gws）
```

## 参考项目

- [OpenCLI](https://github.com/jackwener/OpenCLI) — 浏览器驱动层
- [googleworkspace/cli](https://github.com/googleworkspace/cli) — gws，Gmail API 发送
- [Textual](https://github.com/Textualize/textual) / [rich](https://github.com/Textualize/rich) — TUI 与富文本
- [pi](https://github.com/earendil-works/pi) — 即输即滤选择器交互参考

## License

MIT License，见 [LICENSE](LICENSE)。