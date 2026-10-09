"""补全词表测试 — 斜杠补全词表随命令注册表动态刷新（n10 迁移版）

回归背景:补全器原先缓存词表,`/reload` 只换 Agent、不重建补全器,
插件注入的命令(/task)永远不出现在 Tab 补全里。prompt_toolkit 退场后,
补全逻辑收编至 fp_cli.completion.slash_matches —— 每次调用现查
portal.run.commands（惰性,不缓存),本测试锁定该行为。

原文件: test_slash_completer.py(SlashCompleter/prompt_toolkit 版)。
"""

import os
import sys
import unittest
from types import ModuleType

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_cli.completion import slash_matches
from fp_core.commands import register_command, unregister_command


def _words(text: str) -> list[str]:
    return [w for w, _ in slash_matches(text)]


def _fake_command() -> ModuleType:
    mod = ModuleType("zzfake")
    mod.name = "zzfake"  # type: ignore[attr-defined]
    mod.aliases = []  # type: ignore[attr-defined]
    mod.description = "假命令"  # type: ignore[attr-defined]

    def execute(state: object, arg: str) -> tuple[bool, str]:
        del state, arg
        return (True, "ok")

    mod.execute = execute  # type: ignore[attr-defined]
    return mod


class TestSlashCompletion(unittest.TestCase):
    def test_reflects_command_registered_after_construction(self):
        """构造之后注入的命令必须立即出现（惰性现查,不缓存词表）"""
        self.assertNotIn("/zzfake", _words("/zz"))

        register_command("zzfake", _fake_command())
        try:
            self.assertIn("/zzfake", _words("/zz"))
            # 描述也一并携带
            matched = dict(slash_matches("/zz"))
            self.assertEqual(matched.get("/zzfake"), "假命令")
        finally:
            unregister_command("zzfake")

        # 注销后同样即时消失
        self.assertNotIn("/zzfake", _words("/zz"))

    def test_non_slash_input_yields_nothing(self):
        """非 "/" 前缀不触发补全"""
        self.assertEqual(slash_matches("hello"), [])

    def test_exact_prefix_filter(self):
        """前缀过滤 + 每项含 '/' 前缀"""
        for word, _desc in slash_matches("/h"):
            self.assertTrue(word.startswith("/h"))
            self.assertNotIn(" ", word)


if __name__ == "__main__":
    unittest.main()
