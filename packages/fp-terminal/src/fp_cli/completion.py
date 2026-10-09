"""斜杠补全词表 — 从命令注册表动态加载（TUI 与测试共用的纯函数）

词条来源：``portal.run.commands``（{name: desc}，无 '/' 前缀）。
惰性拉取：每次匹配都现查注册表，/reload 或插件热启停注入的命令立即可见。
"""

from __future__ import annotations

from fp_core.api import portal


def slash_matches(text: str) -> list[tuple[str, str]]:
    """返回 ``(word, desc)`` 匹配列表。

    仅当 ``text`` 以 '/' 开头时匹配（否则返回空——普通文本不触发补全）。
    ``word`` 含 '/' 前缀；按「命令优先」排序。
    """
    if not text.startswith("/"):
        return []
    try:
        commands = portal.run.commands
    except Exception:  # 实例未开/注册表异常 → 无补全可用，不阻断输入
        return []
    matches = [(f"/{name}", desc or "") for name, desc in commands.items() if f"/{name}".startswith(text)]
    matches.sort(key=lambda w: w[0])
    return matches
