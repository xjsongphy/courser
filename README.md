<div align="center">

# courser

[![Platform](https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-lightgrey.svg)](#)
[![Python](https://img.shields.io/badge/python-%E2%89%A5%203.12-blue.svg)](https://www.python.org/)
[![Built with Textual](https://img.shields.io/badge/built%20with-Textual-green.svg)](https://textual.textualize.io/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](https://github.com/xjsongphy/courser/blob/main/LICENSE)

</div>

## Overview

北京大学补退选课程名额监控工具。它在本机 Chrome 中登录选课系统，按设定间隔读取补退选列表；当符合条件的课程出现空余时，发一封邮件提醒。

它只做查询和提醒，不会替你选课，也不会处理验证码。

> 请只用于自己的账号，并遵守学校的相关规定。登录、查询和翻页之间会保留随机间隔；遇到风控提示或验证码时，程序会停止本轮，而不是继续尝试。

```bash
# 从 PyPI 安装（只带 rich/textual；opencli/gws 需另装，见下方）
pipx install courser        # 推荐：独立 CLI 环境，不污染系统 Python
# 或装入当前 Python 环境：
pip install courser
```

```bash
# 更新
pipx upgrade courser        # 推荐（pipx 安装时用这个）
pip install --upgrade courser   # 或：pip install -U courser
uv tool upgrade courser     # 若当初用 uv tool install
```

> 发布到 PyPI 只包含 courser 自身与 `rich`/`textual` 两个 Python 依赖。
> **opencli / gws 不随包发布，需在本机单独安装**（见下方 Quick start 的说明）。

## Quick start

需要：Python 3.12+、[uv](https://docs.astral.sh/uv/)、Chrome，以及两个已完成配置的命令行工具：

- [OpenCLI](https://github.com/jackwener/OpenCLI)：连接本机 Chrome。安装扩展、启动 daemon 后，运行 `opencli doctor`，确认检查通过。
- [Google Workspace CLI](https://github.com/googleworkspace/cli)（`gws`）：发送提醒邮件。
  安装：`npm install -g @googleworkspace/cli`。
  首次先执行 `gws auth setup`（一次性初始化 Google Cloud 项目 / OAuth 配置 / 启用 API），
  再执行 `gws auth login` 授权 Gmail；之后 token 失效时重新 `gws auth login` 即可。

```bash
git clone https://github.com/xjsongphy/courser
cd courser
uv sync
uv run courser
```

第一次启动会打开设置向导：填写收件邮箱保存后会自动生成 `config.json`（**无需手动复制**），其余设置可在程序的设置页完成。

若你更愿意由 Chrome 或密码管理器填充学号和密码，保持 `credentials` 为空即可；也可以写入 `config.json`，或通过环境变量 `PKU_USERNAME`、`PKU_PASSWORD` 提供。

## What it does

- 每轮从 IAAA 登录，打开补退选页面，并按网站实际页数读取课程；不依赖固定页数。
- 按课程名、课程类别和开课院系筛选。可选择任一条件命中或全部条件命中。
- 只有“限数大于已选”的课程才会提醒；同一门课有冷却时间，邮件也有每小时上限。
- 提供一个终端界面查看最近结果、调整筛选和设置、查看日志。文字选择风格为 Textual
  自接管：鼠标拖动选择、松手即自动复制到剪贴板（OSC 52），并 toast 提示；滚轮滚
  页面，帮助 / 日志 / 详情页均可用。

## Run one check

```bash
uv run courser --once
```

这会跑完一轮并把结果打印到终端，适合确认登录和筛选是否正常。持续运行则直接执行 `uv run courser`。

## Configuration

`config.json` 会在首次启动向导完成时自动生成，**一般情况下无需手动修改**——轮询节奏、筛选条件、收件邮箱等都可直接在 TUI 的设置页修改并自动保存。以下为完整的配置结构说明（参考）：

```jsonc
{
  "interval_min": 8.0,          // 查询间隔（分钟）
  "interval_jitter": 0.3,       // 间隔随机抖动
  "page_delay_min": 0.8,        // 翻页最小等待（秒，人类节奏）
  "page_delay_max": 2.0,        // 翻页最大等待（秒）
  "session": "courser-watch",   // opencli 浏览器会话名
  "window": "background",       // 浏览器窗口模式
  "force_relogin": true,        // 每轮是否强制重新登录
  "credentials": {              // 可留空（依赖浏览器密码管理器自动填充）
    "username": "",
    "password": ""
  },
  "filters": {
    "names": [],               // 课程名，子串匹配
    "categories": [],          // 课程类别
    "depts": ["英语语言文学系"],
    "match": "any"            // any：任一条件命中；all：全部条件命中
  },
  "notify": {
    "to": "you@example.com",   // 收件邮箱（必填）
    "gws_from": "",            // gws 发件账号（可选，默认取认证账号）
    "min_interval_min": 15.0,  // 同一门课的通知冷却（分钟）
    "max_per_hour": 5          // 每小时发送上限
  }
}
```

`config.json` 不会提交到 Git。运行日志写入 `data/courser.log`。

## Troubleshooting

`opencli doctor` 需要全部通过。若登录页填入账号密码后仍未跳转，先在同一个 Chrome 中手动完成一次登录，再执行 `uv run courser --once`。验证码和二次验证需要你本人处理，courser 不会代填。

## Development

```bash
uv run python tests/unit/test_opencli.py
uv run python tests/integration/test_round_runner.py
uv run python tests/tui/test_navigation.py
uv run python tests/tui/test_responsive.py
```

真实网站的端到端检查仍需手动执行：

```bash
uv run courser --once
```

## License

[MIT](LICENSE)
