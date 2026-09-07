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
import re
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
    """从 stdout 提取 JSON（对象/数组；opencli 可能在 JSON 前后附带零散行）。"""
    starts = [i for i in (stdout.find("{"), stdout.find("[")) if i >= 0]
    if not starts:
        raise OpenCliError([], stdout, "stdout 中未找到 JSON", 1)
    start = min(starts)
    end = max(stdout.rfind("}"), stdout.rfind("]"))
    if end <= start:
        raise OpenCliError([], stdout, f"stdout 中未找到 JSON 闭合 (start={start})", 1)
    try:
        return json.loads(stdout[start:end + 1])
    except json.JSONDecodeError as exc:
        raise OpenCliError([], stdout, f"JSON 解析失败: {exc}", 1) from exc


_TRUE_FALSE = {"true": True, "false": False, "null": None}
_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def _parse_eval_output(stdout: str) -> Any:
    """把 opencli eval 的 stdout 还原为 JS 值。

    关键坑（实测踩过）：JS 布尔/数字经 opencli 回显为裸字符串 "true"/"false"/"42"，
    若不还原成 Python 的 True/False，`x is True` 类判断永远为 False，
    会导致"登录页/工作页在位校验"恒假 → 登录永远失败。
    """
    stripped = stdout.strip()
    if stripped in _TRUE_FALSE:
        return _TRUE_FALSE[stripped]
    if _NUM_RE.match(stripped):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            return stripped
    if stripped.startswith("{") or stripped.startswith("["):
        return _extract_json(stripped)
    return stripped


def open(session: str, url: str, window: Optional[str] = None) -> dict:
    """在当前会话标签中打开 URL。"""
    return _extract_json(_run(session, ["open", url], window=window)[0])


def get_url(session: str) -> str:
    """返回当前标签页 URL（纯文本）。"""
    return _run(session, ["get", "url"])[0].strip()


def eval_js(session: str, js: str) -> Any:
    """在页面内执行只读 JS，返回解析后的值（布尔/数字/对象/字符串均正确处理）。"""
    out, _ = _run(session, ["eval", js])
    return _parse_eval_output(out)


def click(session: str, target: str, nth: Optional[int] = None) -> dict:
    args = ["click", target]
    if nth is not None:
        args += ["--nth", str(nth)]
    return _extract_json(_run(session, args)[0])


def click_by(session: str, *, role: Optional[str] = None, name: Optional[str] = None,
             nth: Optional[int] = None) -> bool:
    """按语义（可访问性角色/名称）点击，如翻页 Next 链接。
    用途：用"点击"而不是直接改 URL 跳页（后者易触发风控提示）。"""
    args = ["click"]
    if role:
        args += ["--role", role]
    if name:
        args += ["--name", name]
    if nth is not None:
        args += ["--nth", str(nth)]
    env = _extract_json(_run(session, args)[0])
    return bool(env.get("clicked"))


def fill(session: str, target: str, text: str) -> dict:
    """以精确替换的方式写入输入框（不会触发自动补全/键盘事件），并校验。"""
    return _extract_json(_run(session, ["fill", target, text])[0])


def state(session: str) -> dict:
    return _extract_json(_run(session, ["state"])[0])


def tab_list(session: str) -> list:
    """列出会话内的标签页 [{page,url,title,...}, ...]。"""
    out, _ = _run(session, ["tab", "list"])
    try:
        return _extract_json(out)
    except OpenCliError:
        return []


def tab_select(session: str, page: str) -> dict:
    """切换到指定标签页（page 为 tab list 里的标识）。"""
    return _extract_json(_run(session, ["tab", "select", page])[0])


def wait_selector(session: str, selector: str, timeout_ms: int = 15000) -> dict:
    return _extract_json(_run(session, ["wait", "selector", selector, "--timeout", str(timeout_ms)])[0])


def console(session: str) -> str:
    """读取最近的浏览器控制台消息（用于登录失败诊断）。"""
    return _run(session, ["console"])[0].strip()


def keys(session: str, key: str) -> dict:
    """向当前聚焦元素发送按键（如 Enter）。"""
    return _extract_json(_run(session, ["keys", key])[0])