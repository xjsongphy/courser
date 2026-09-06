"""opencli 命令行的薄封装。

所有浏览器交互都通过 `opencli browser <session> <command>` 完成：
- 页面打开/跳转:  open / click / back
- 读取:          get url / eval / state
- 写入:          fill / click（登录按钮等；绝不提交表单抢课）

返回值解析：
- JSON 命令（eval / click / fill / state ...）→ 解析 stdout 中的 JSON 对象
- 纯文本命令（get url / get title）→ 返回 stdout 原文
opencli 的升级提示、代理警告等噪音输出在 stderr，不影响 stdout 解析。
"""

from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Optional

OPENCLI = os.environ.get("OPENCLI_BIN", "opencli")

# 单条浏览器命令的超时（秒）；页面加载慢时可适当调大
_CMD_TIMEOUT = int(os.environ.get("OPENCLI_CMD_TIMEOUT", "120"))


class OpenCliError(RuntimeError):
    """opencli 命令失败（非零退出码或 stdout 无法解析）。"""

    def __init__(self, cmd: list[str], stdout: str, stderr: str, code: int):
        self.cmd = cmd
        self.stdout = stdout
        self.stderr = stderr
        self.code = code
        super().__init__(f"opencli 命令失败 (exit={code}): {' '.join(cmd)}\nstderr: {stderr[-400:]}")


def _sanitize_env():
    env = dict(os.environ)
    env.setdefault("OPENCLI_BROWSER_CONNECT_TIMEOUT", "60")
    env.setdefault("OPENCLI_BROWSER_COMMAND_TIMEOUT", str(_CMD_TIMEOUT))
    return env


def _run(session: str, args: list[str], window: Optional[str] = None,
         timeout: Optional[int] = None) -> tuple[str, str]:
    cmd = [OPENCLI, "browser", session, *args]
    if window:
        cmd += ["--window", window]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout or _CMD_TIMEOUT,
        env=_sanitize_env(),
    )
    out, err = proc.stdout or "", proc.stderr or ""
    if proc.returncode != 0:
        raise OpenCliError(cmd, out, err, proc.returncode)
    return out, err


def _extract_json(stdout: str) -> Any:
    """从 stdout 提取 JSON（opencli 可能在 JSON 前后附带零散行）。"""
    start, end = stdout.find("{"), stdout.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise OpenCliError([], stdout, "stdout 中未找到 JSON 对象", 1)
    try:
        return json.loads(stdout[start:end + 1])
    except json.JSONDecodeError as exc:
        raise OpenCliError([], stdout, f"JSON 解析失败: {exc}", 1) from exc


def open(session: str, url: str, window: Optional[str] = None) -> dict:
    """在当前会话标签中打开 URL。"""
    return _extract_json(_run(session, ["open", url], window=window)[0])


def get_url(session: str) -> str:
    """返回当前标签页 URL（纯文本）。"""
    return _run(session, ["get", "url"])[0].strip()


def eval_js(session: str, js: str) -> Any:
    """在页面内执行只读 JS，返回解析后的值（IIFE 应返回 JSON）。"""
    out, _ = _run(session, ["eval", js])
    stripped = out.strip()
    if stripped.startswith("{"):
        return _extract_json(stripped)
    return stripped


def click(session: str, target: str, nth: Optional[int] = None) -> dict:
    args = ["click", target]
    if nth is not None:
        args += ["--nth", str(nth)]
    return _extract_json(_run(session, args)[0])


def fill(session: str, target: str, text: str) -> dict:
    """以精确替换的方式写入输入框（不会触发自动补全/键盘事件），并校验。"""
    return _extract_json(_run(session, ["fill", target, text])[0])


def state(session: str) -> dict:
    return _extract_json(_run(session, ["state"])[0])


def wait_selector(session: str, selector: str, timeout_ms: int = 15000) -> dict:
    return _extract_json(_run(session, ["wait", "selector", selector, "--timeout", str(timeout_ms)])[0])