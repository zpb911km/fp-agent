"""斜杠补全词表 — 从命令注册表动态加载（TUI 与测试共用的纯函数）

词条来源：``portal.run.command_catalog``（名字/描述/别名/docstring，
唯一接口组下发，前端不碰 ``fp_core.commands``）。
惰性拉取：每次匹配都现查注册表，/reload 或插件热启停注入的命令立即可见。

帮助正文从 docstring 里抽「用法:」小节（命令层约定的用户面文字），
抽不到就留空：浮层少一行说明是可接受的降级，绝不能因此抛错。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from fp_core.api import portal

#: 帮助正文保留的最大行数——浮层高度有限，超出部分在数据层就裁掉并**留可见标记**
#: （见 :func:`clip_lines`），不做静默丢弃。
HELP_MAX_LINES = 6

#: 「用法:」小节里的对齐空格（``/back list            看列表``）在窄栏折行会散架，
#: 收成分隔符再折行，读起来才是一句话。
_GAP = re.compile(r"\s{2,}")
_USAGE_HEAD = ("用法:", "用法：")


@dataclass(frozen=True, slots=True)
class Candidate:
    """一条补全候选（TUI 浮层的数据单元）。"""

    word: str
    """含 '/' 前缀的命令名，如 ``/compact``。"""

    desc: str
    """命令模块声明的 ``description``（一行，可能为空）。"""

    aliases: tuple[str, ...] = ()
    """别名（不含 '/'）。"""

    help_text: str = ""
    """多行帮助正文（docstring 的「用法:」小节，已折成分隔符形式）；可能为空。"""


def _usage_block(doc: str) -> str:
    """从命令模块 docstring 里抽「用法:」小节（命令层约定的**用户面**文字）。

    docstring 的其余部分是写给开发者的实现说明（「不再经过 Agent 中转」之类），
    不该出现在补全浮层；没有「用法:」小节的命令就只显示一行 description。
    """
    lines = doc.splitlines()
    start = next(
        (i + 1 for i, line in enumerate(lines) if line.strip().startswith(_USAGE_HEAD)),
        None,
    )
    if start is None:
        return ""
    body = [_GAP.sub(" · ", line.strip()) for line in lines[start:] if line.strip()]
    return clip_lines(body, HELP_MAX_LINES)


def clip_lines(lines: list[str], limit: int) -> str:
    """留 ``limit`` 行，超出的折成一句「省略 N 行」——裁切必须可见。"""
    if len(lines) <= limit:
        return "\n".join(lines)
    kept = lines[: max(1, limit - 1)]
    return "\n".join([*kept, f"…（省略 {len(lines) - len(kept)} 行）"])


def slash_candidates(text: str) -> list[Candidate]:
    """返回前缀匹配 ``text`` 的候选列表。

    仅当 ``text`` 以 '/' 开头时匹配（否则返回空——普通文本不触发补全）。
    ``word`` 含 '/' 前缀；按命令名排序。
    """
    if not text.startswith("/"):
        return []
    try:
        catalog = portal.run.command_catalog  # 已按命令名排序
    except Exception:  # 实例未开/注册表异常 → 无补全可用，不阻断输入
        return []
    out: list[Candidate] = []
    for info in catalog:
        word = f"/{info.name}"
        if not word.startswith(text):
            continue
        out.append(
            Candidate(
                word=word,
                desc=info.desc,
                aliases=tuple(info.aliases),
                help_text=_usage_block(info.doc),
            )
        )
    return out


def slash_matches(text: str) -> list[tuple[str, str]]:
    """``(word, desc)`` 兼容视图——旧签名，调用方按二元组解包。"""
    return [(c.word, c.desc) for c in slash_candidates(text)]
