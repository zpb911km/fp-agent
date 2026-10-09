"""输入历史持久化 — prompt_toolkit FileHistory 格式兼容读写（零依赖）

格式（与 prompt_toolkit FileHistory 一致）：每个条目以 ``+<timestamp>`` 行开头，
随后是条目内容行。兼容读取旧文件（直接沿用原 ``INPUT_HISTORY_FILE``），
向前兼容：本模块写入的文件 prompt_toolkit 也能读。
"""

from __future__ import annotations

import os
import time


def load_history(path: str, *, limit: int = 500) -> list[str]:
    """读取历史条目（旧→新），过滤空条目，最多返回 ``limit`` 条。"""
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return []
    entries: list[str] = []
    cur: list[str] | None = None
    for line in lines:
        if line.startswith("+"):
            if cur is not None:
                text = "\n".join(cur).rstrip()
                if text:
                    entries.append(text)
            cur = []
        elif cur is not None:
            cur.append(line)
    if cur is not None:
        text = "\n".join(cur).rstrip()
        if text:
            entries.append(text)
    return entries[-limit:]


def append_history(path: str, text: str) -> None:
    """追加一条历史（FileHistory 兼容格式）。静默容错：写失败不打断输入。"""
    text = text.rstrip("\n")
    if not text.strip():
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"+{int(time.time())}\n")
            f.write(text + "\n")
    except OSError:
        pass
