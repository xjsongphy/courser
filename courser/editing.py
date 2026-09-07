"""可复用的行内字段编辑器（设置页 / 首启向导等所有输入共用）。

它不依赖任何具体 UI 控件：只维护一段文本缓冲 + 光标位置，外部把按键的原始信息
喂进来（key / char / printable），它处理移动光标、删除、在光标处插入并返回
'commit' / 'cancel' 让调用方决定何时落盘。渲染也由它统一负责（含密码打码与
块状光标 ▌），保证各处输入外观与行为完全一致——原值保留、不清空重输、键盘
移动光标、支持中文输入。
"""

from __future__ import annotations

from rich.markup import escape


class FieldEditor:
    """一个复用的行内单行编辑器实例。

    用法：
        ed.begin(value, kind="text")        # 进入编辑，光标置于末尾
        out = ed.feed(key, char, printable)  # 每来一个键调用一次
        # out 为 'commit' / 'cancel' 时由调用方落盘或放弃；其余在内部处理
        markup = ed.markup()                 # 渲染带光标的文本（供页面显示）
        ed.reset()                           # 结束编辑
    """

    CURSOR = "▌"

    def __init__(self) -> None:
        self.text = ""
        self.caret = 0
        self.kind = "text"        # text | password
        self.active = False

    # -- 生命周期 -----------------------------------------------------
    def begin(self, value: str, kind: str = "text") -> None:
        """开始编辑：原值保留，光标默认在末尾（追加式修改）。"""
        self.active = True
        self.text = value
        self.kind = kind
        self.caret = len(value)

    def reset(self) -> None:
        self.active = False
        self.text = ""
        self.caret = 0

    # -- 取值 ---------------------------------------------------------
    def markup(self) -> str:
        """渲染当前缓冲：密码打码为 •，光标处显示块状 ▌（转义后安全）。"""
        shown = self.text if self.kind != "password" else "•" * len(self.text)
        c = max(0, min(self.caret, len(shown)))
        return ("[cyan]" + escape(shown[:c]) + self.CURSOR +
                escape(shown[c:]) + "[/]")

    def _insert(self, text: str) -> None:
        self.text = self.text[:self.caret] + text + self.text[self.caret:]
        self.caret += len(text)

    def _backspace(self) -> None:
        if self.caret > 0:
            self.text = self.text[:self.caret - 1] + self.text[self.caret:]
            self.caret -= 1

    def _delete(self) -> None:
        if self.caret < len(self.text):
            self.text = self.text[:self.caret] + self.text[self.caret + 1:]

    def feed(self, key: str, char: str | None,
             printable: bool) -> str | None:
        """处理一次按键，返回 'commit' / 'cancel' / None。

        key / char / printable 取自某个 TUI 的按键事件；本方法只做纯文本编辑，
        不落盘、不渲染——这样任何调用方（设置页、首启向导…）都能复用。
        """
        if not self.active:
            return None
        if key == "enter":
            return "commit"
        if key == "escape":
            return "cancel"
        if key == "left":
            self.caret = max(0, self.caret - 1)
        elif key == "right":
            self.caret = min(len(self.text), self.caret + 1)
        elif key == "home":
            self.caret = 0
        elif key == "end":
            self.caret = len(self.text)
        elif key == "backspace":
            self._backspace()
        elif key == "delete":
            self._delete()
        else:
            # 可打印字符在光标处插入（含中文）。Textual 里字母常只有 key 无 char，
            # 这里统一回退到单字符的 key。
            t = char if char else (key if len(key) == 1 else None)
            if t and printable:
                self._insert(t)
        return None
