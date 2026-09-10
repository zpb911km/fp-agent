"""SlashCompleter 测试 — 斜杠补全词表随命令注册表动态刷新

回归背景:补全器在 REPL 启动时构建一次,`/reload` 只换 Agent、不重建补全器。
若词表被缓存,插件注入的命令(如 `/task`)将永远不出现在 Tab 补全里。
故 get_completions 每次惰性重载 —— 本测试锁定该行为。
"""

import os
import sys
import unittest
from types import ModuleType

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document

from fp_cli.main import SlashCompleter
from fp_core.commands import register_command, unregister_command


def _completions(completer: SlashCompleter, text: str) -> list[str]:
    """对 text 取补全词条(光标置于末尾)"""
    doc = Document(text, len(text))
    return [c.text for c in completer.get_completions(doc, CompleteEvent())]


def _fake_command() -> ModuleType:
    mod = ModuleType("zzfake")
    mod.name = "zzfake"  # type: ignore[attr-defined]
    mod.aliases = []  # type: ignore[attr-defined]
    mod.description = "假命令"  # type: ignore[attr-defined]

    def execute(state: object, arg: str) -> tuple[bool, str]:
        return (True, "ok")

    mod.execute = execute  # type: ignore[attr-defined]
    return mod


class TestSlashCompleter(unittest.TestCase):
    def test_reflects_command_registered_after_construction(self):
        """构造补全器之后注入的命令,必须立即出现在补全中(惰性刷新)"""
        completer = SlashCompleter()
        self.assertNotIn("/zzfake", _completions(completer, "/zz"))

        register_command("zzfake", _fake_command())
        try:
            self.assertIn("/zzfake", _completions(completer, "/zz"))
        finally:
            unregister_command("zzfake")

        # 注销后同样即时消失
        self.assertNotIn("/zzfake", _completions(completer, "/zz"))

    def test_non_slash_input_yields_nothing(self):
        """非 "/" 前缀不触发补全"""
        self.assertEqual(_completions(SlashCompleter(), "hello"), [])


if __name__ == "__main__":
    unittest.main()
