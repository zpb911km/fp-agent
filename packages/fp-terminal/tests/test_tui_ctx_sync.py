"""上下文对账 — 视图 == transcript 非 system 投影（tui_ctxview + FPApp._sync_view）

锁定的契约：
- 投影层：首条 system（系统提示词）不进视图，其余消息**一律**产出单元，未知
  role/结构走兜底而不是丢弃——漏一条，视图就不再等于上下文。
- 对账层：判据是「当前投影是否仍是已确认投影的尾部追加」。纯追加 → 不动视图；
  前缀断裂（中段删块 / 换内容 / 换源）→ 重画。
- 时机层：流式在途（busy / 正文在写 / 思考卡活跃）不重画，只记账，收尾补。

全部用例不认任何命令名或工具名——这正是机制要守住的性质：新增改写路径
（新命令 / 新工具 / 插件 / reload / 外部客户端）无需改这里。
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_cli import tui_ctxview as ctxview
from fp_cli.tui import TUIIO, FPApp

# ══════════════════════════════════════════════════════════════
# 投影层（纯函数）
# ══════════════════════════════════════════════════════════════


def _msg(role: str, content: Any = "", **extra: Any) -> dict[str, Any]:
    return {"role": role, "content": content, **extra}


class TestProjection(unittest.TestCase):
    """transcript → 单元序列"""

    def test_first_system_is_prompt_boundary(self) -> None:
        units = ctxview.project([_msg("system", "SP"), _msg("user", "hi")])
        self.assertEqual([u.kind for u in units], [ctxview.KIND_USER])
        self.assertEqual(units[0].text, "hi")

    def test_later_system_is_rendered_not_dropped(self) -> None:
        """后续 system 是上下文的一部分（如 ⁂[kind] 注入标记）→ 必须画出来"""
        units = ctxview.project([_msg("system", "SP"), _msg("system", "⁂[inject] 标记")])
        self.assertEqual([u.kind for u in units], [ctxview.KIND_OTHER])
        self.assertEqual(units[0].name, "system")
        self.assertIn("标记", units[0].text)

    def test_empty_transcript(self) -> None:
        self.assertEqual(ctxview.project([]), [])
        self.assertEqual(ctxview.project(None), [])

    def test_assistant_expands_think_content_tool_calls_in_order(self) -> None:
        units = ctxview.project([
            _msg(
                "assistant",
                "正文",
                reasoning_content="想一下",
                tool_calls=[{"id": "c1", "function": {"name": "bash", "arguments": '{"command": "ls"}'}}],
            )
        ])
        self.assertEqual(
            [u.kind for u in units],
            [ctxview.KIND_THINK, ctxview.KIND_ASSISTANT, ctxview.KIND_TOOL_CALL],
        )
        self.assertEqual(units[0].chars, len("想一下"))
        self.assertEqual(units[2].name, "bash")
        self.assertEqual(units[2].args, {"command": "ls"})  # JSON 字符串已解析

    def test_tool_call_flat_shape(self) -> None:
        """兼容扁平形状 {name, args}（非 OpenAI 嵌套 function）"""
        units = ctxview.project([_msg("assistant", "", tool_calls=[{"name": "grep", "args": {"q": "x"}}])])
        self.assertEqual(units[0].kind, ctxview.KIND_TOOL_CALL)
        self.assertEqual(units[0].name, "grep")
        self.assertEqual(units[0].args, {"q": "x"})

    def test_tool_call_id_preferred_over_args(self) -> None:
        """同工具同参数被调两次：只有 id 能区分——key 必须用 id"""
        same_args = '{"command": "ls"}'
        units = ctxview.project([
            _msg(
                "assistant",
                "",
                tool_calls=[
                    {"id": "c1", "function": {"name": "bash", "arguments": same_args}},
                    {"id": "c2", "function": {"name": "bash", "arguments": same_args}},
                ],
            )
        ])
        self.assertEqual(len({u.key for u in units}), 2)

    def test_tool_call_unparsable_arguments_kept(self) -> None:
        units = ctxview.project([
            _msg("assistant", "", tool_calls=[{"id": "c", "function": {"name": "x", "arguments": "{坏 JSON"}}])
        ])
        self.assertEqual(units[0].args, "{坏 JSON")  # 原文保留，不吞

    def test_malformed_tool_call_rendered(self) -> None:
        """tool_calls 里出现非 dict 元素：兜底渲染，不静默丢"""
        units = ctxview.project([_msg("assistant", "", tool_calls=["裸字符串"])])
        self.assertEqual(units[0].kind, ctxview.KIND_OTHER)
        self.assertIn("裸字符串", units[0].text)

    def test_empty_assistant_still_takes_a_slot(self) -> None:
        units = ctxview.project([_msg("assistant", "")])
        self.assertEqual([u.kind for u in units], [ctxview.KIND_ASSISTANT])
        self.assertEqual(units[0].text, "")

    def test_tool_result_uses_call_id(self) -> None:
        units = ctxview.project([_msg("tool", "输出", tool_call_id="c1")])
        self.assertEqual(units[0].kind, ctxview.KIND_TOOL_RESULT)
        self.assertEqual(units[0].key, "tool_result:c1")

    def test_unknown_role_goes_to_fallback(self) -> None:
        units = ctxview.project([_msg("weird", "内容")])
        self.assertEqual(units[0].kind, ctxview.KIND_OTHER)
        self.assertEqual(units[0].name, "weird")
        self.assertEqual(units[0].text, "内容")

    def test_missing_role_goes_to_fallback(self) -> None:
        units = ctxview.project([{"content": "无角色"}])
        self.assertEqual(units[0].kind, ctxview.KIND_OTHER)
        self.assertEqual(units[0].name, "unknown")

    def test_non_dict_message_rendered(self) -> None:
        units = ctxview.project(["裸元素"])
        self.assertEqual(units[0].kind, ctxview.KIND_OTHER)
        self.assertIn("裸元素", units[0].text)

    def test_content_parts_rendered_as_json(self) -> None:
        """结构化 content（多模态 parts）不被当成空串吞掉"""
        parts = [{"type": "text", "text": "hi"}]
        units = ctxview.project([_msg("user", parts)])
        self.assertIn("hi", units[0].text)

    def test_keys_are_content_bound(self) -> None:
        a = ctxview.project([_msg("user", "同一句")])
        b = ctxview.project([_msg("user", "同一句")])
        c = ctxview.project([_msg("user", "另一句")])
        self.assertEqual(a[0].key, b[0].key)  # 同内容 → 同指纹（跨进程稳定）
        self.assertNotEqual(a[0].key, c[0].key)

    def test_middle_deletion_keeps_remaining_keys(self) -> None:
        """中段删块后剩余单元的 key 必须不变——前缀对齐的前提"""
        msgs = [_msg("user", "u1"), _msg("assistant", "a1"), _msg("user", "u2"), _msg("assistant", "a2")]
        before = ctxview.fingerprint(ctxview.project(msgs))
        after = ctxview.fingerprint(ctxview.project([msgs[0], msgs[2], msgs[3]]))
        self.assertEqual(after[0], before[0])
        self.assertEqual(after[1:], before[2:])


class TestAppendOnly(unittest.TestCase):
    """对账判据：纯尾部追加 vs 前缀断裂"""

    def _sig(self, *texts: str) -> tuple[str, ...]:
        return ctxview.fingerprint(ctxview.project([_msg("assistant", t) for t in texts]))

    def test_append_only(self) -> None:
        self.assertTrue(ctxview.is_append_only(self._sig("a", "b"), self._sig("a", "b", "c")))

    def test_same_sequence(self) -> None:
        self.assertTrue(ctxview.is_append_only(self._sig("a"), self._sig("a")))

    def test_tail_deletion_is_not_append(self) -> None:
        """尾部被删（退化最近块）→ 视图里有已消失的卡 → 必须重画"""
        self.assertFalse(ctxview.is_append_only(self._sig("a", "b"), self._sig("a")))

    def test_middle_deletion_is_not_append(self) -> None:
        self.assertFalse(ctxview.is_append_only(self._sig("a", "b", "c"), self._sig("a", "c")))

    def test_content_replacement_is_not_append(self) -> None:
        self.assertFalse(ctxview.is_append_only(self._sig("a", "b"), self._sig("a", "B")))

    def test_middle_insertion_is_not_append(self) -> None:
        self.assertFalse(ctxview.is_append_only(self._sig("a", "b"), self._sig("a", "x", "b")))

    def test_source_switch_is_not_append(self) -> None:
        self.assertFalse(ctxview.is_append_only(self._sig("a", "b"), self._sig("x", "y")))

    def test_empty_baseline_with_content_is_not_append(self) -> None:
        """空基线 + 非空投影 → 必须重画（新会话里 /resume 的真实形态）

        回归：``current[:0] == ()`` 恒真，曾把任何换源误判成「纯尾部追加」，
        基线被静默推进而视图永不重建。
        """
        self.assertFalse(ctxview.is_append_only((), self._sig("x", "y")))

    def test_empty_baseline_and_empty_projection_agree(self) -> None:
        """空基线 + 空投影 = 一致（新会话刚启动，无内容可画）"""
        self.assertTrue(ctxview.is_append_only((), ()))

    def test_nonempty_baseline_vs_empty_projection_is_not_append(self) -> None:
        """上下文被清空（/new、/clear）→ 视图里还有旧卡 → 必须清屏重画"""
        self.assertFalse(ctxview.is_append_only(self._sig("a"), ()))

    def test_message_count_skips_prompt(self) -> None:
        msgs = [_msg("system", "SP"), _msg("user", "hi"), _msg("assistant", "yo")]
        self.assertEqual(ctxview.message_count(msgs), 2)
        self.assertEqual(ctxview.message_count([]), 0)


# ══════════════════════════════════════════════════════════════
# 对账层（pilot，桩 portal）
# ══════════════════════════════════════════════════════════════


class _StubStatus:
    model = "stub-model"
    session_id = "sess-0123456789abcdef"
    is_processing = False
    is_run_active = False
    cancelled = False


class _StubPortal:
    """portal 桩：transcript 可现场改写，忙碌态可切换。"""

    def __init__(self, messages: list[dict[str, Any]] | None = None) -> None:
        self.messages: list[dict[str, Any]] = list(messages or [])
        self.busy = False
        self._status = _StubStatus()

    @property
    def is_open(self) -> bool:
        return True

    @property
    def run(self) -> _StubPortal:
        return self

    @property
    def status(self) -> _StubStatus:
        self._status.is_run_active = self.busy
        return self._status

    @property
    def transcript(self) -> list[dict[str, Any]]:
        return list(self.messages)


class _SyncHarness(unittest.TestCase):
    """在 pilot 里跑一次场景：initial transcript → 挂载 → action(fake, app)"""

    def drive(self, initial: list[dict[str, Any]], action: Any) -> dict[str, Any]:
        from fp_core.api import portal as real_portal

        fake = _StubPortal(initial)
        out: dict[str, Any] = {"fake": fake}

        async def _scenario() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            with (
                patch.object(type(real_portal), "is_open", property(lambda self: True)),
                patch.object(real_portal, "run", fake, create=True),
            ):
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    out["value"] = action(fake, app)
                    await pilot.pause(0.05)
                    out["app"] = app

        asyncio.run(_scenario())
        return out


class TestSyncView(_SyncHarness):
    """FPApp._sync_view：追加不动 / 断裂重画 / 在途延后"""

    def _children(self, app: FPApp) -> list[Any]:
        return list(app.query_one("#chat").children)

    def test_baseline_marks_settled_without_redraw(self) -> None:
        """挂载后立基线：默认对账不重画、不报错"""
        initial = [_msg("system", "SP"), _msg("user", "hi"), _msg("assistant", "yo")]

        def action(fake: _StubPortal, app: FPApp) -> bool:
            return app._sync_view()  # noqa: SLF001

        out = self.drive(initial, action)
        self.assertFalse(out["value"])

    def test_append_only_does_not_redraw(self) -> None:
        initial = [_msg("system", "SP"), _msg("user", "hi")]

        def action(fake: _StubPortal, app: FPApp) -> tuple[bool, int, int]:
            before = len(self._children(app))
            fake.messages.append(_msg("assistant", "yo"))
            redrawn = app._sync_view()  # noqa: SLF001
            return redrawn, before, len(self._children(app))

        redrawn, before, after = self.drive(initial, action)["value"]
        self.assertFalse(redrawn)  # 纯追加：事件流已渲染，不该动视图
        self.assertEqual(before, after)

    def test_middle_rewrite_redraws_and_notes(self) -> None:
        """中段删块（退化历史块的形态）→ 重画 + 可见提示"""
        initial = [
            _msg("system", "SP"),
            _msg("user", "u1"),
            _msg("assistant", "a1"),
            _msg("user", "u2"),
            _msg("assistant", "a2"),
        ]

        def action(fake: _StubPortal, app: FPApp) -> bool:
            del fake.messages[2]  # 删掉 a1：不认来源，只认投影断裂
            redrawn = app._sync_view()  # noqa: SLF001
            out["children"] = self._children(app)
            return redrawn

        out: dict[str, Any] = {}
        self.assertTrue(self.drive(initial, action)["value"])
        texts = [str(child.render()) for child in out["children"]]
        self.assertTrue(any("上下文已同步" in t for t in texts), texts)

    def test_source_switch_redraws(self) -> None:
        """/resume 形态：整表换源（旧投影与新投影毫无交集）"""
        initial = [_msg("system", "SP"), _msg("user", "旧会话消息")]

        def action(fake: _StubPortal, app: FPApp) -> bool:
            fake.messages = [_msg("system", "SP2"), _msg("user", "新会话消息")]
            return app._sync_view()  # noqa: SLF001

        self.assertTrue(self.drive(initial, action)["value"])

    def test_empty_baseline_source_switch_redraws(self) -> None:
        """新会话（只有 system，基线为空）里 /resume → 必须重建视图

        真实场景回归：TUI 启动于新会话时基线为空，换源曾被判成「纯尾部追加」，
        视图停在欢迎屏不动。
        """
        initial = [_msg("system", "SP")]  # project → 空投影 → settled == ()

        def action(fake: _StubPortal, app: FPApp) -> tuple[bool, list[str]]:
            fake.messages = [_msg("system", "SP2"), _msg("user", "目标会话内容")]
            redrawn = app._sync_view()  # noqa: SLF001
            return redrawn, [str(c.render()) for c in app.query_one("#chat").children]

        out = self.drive(initial, action)["value"]
        redrawn, texts = out
        self.assertTrue(redrawn)
        self.assertTrue(any("目标会话内容" in t for t in texts), texts)

    def test_mount_renders_existing_history(self) -> None:
        """启动即带上下文（-r 续会话）→ 历史立刻进视图，不等第一次断裂"""
        initial = [_msg("system", "SP"), _msg("user", "历史第一句"), _msg("assistant", "历史回答")]

        def action(fake: _StubPortal, app: FPApp) -> list[str]:
            # Markdown.render() 是异步的（同步调用只回 Blank），故并取 source
            return [str(c.render()) + str(getattr(c, "source", "")) for c in app.query_one("#chat").children]

        texts = self.drive(initial, action)["value"]
        self.assertTrue(any("历史第一句" in t for t in texts), texts)
        self.assertTrue(any("历史回答" in t for t in texts), texts)

    def test_busy_defers_then_flushes(self) -> None:
        """在途不重画：只记账；busy 落定后的对账补齐"""
        initial = [_msg("system", "SP"), _msg("user", "u1"), _msg("assistant", "a1")]

        def action(fake: _StubPortal, app: FPApp) -> tuple[bool, bool, bool]:
            fake.busy = True
            fake.messages = [_msg("system", "SP"), _msg("user", "u1")]
            first = app._sync_view()  # noqa: SLF001 — 忙碌：记账不重画
            pending = app._sync_pending  # noqa: SLF001
            fake.busy = False
            second = app._sync_view()  # noqa: SLF001 — 收尾拍补
            return first, pending, second

        first, pending, second = self.drive(initial, action)["value"]
        self.assertFalse(first)
        self.assertTrue(pending)
        self.assertTrue(second)

    def test_live_stream_defers(self) -> None:
        """正文在写（_live 未封口）也不重画——会拆掉正在生成的卡"""
        initial = [_msg("system", "SP"), _msg("user", "u1"), _msg("assistant", "a1")]

        def action(fake: _StubPortal, app: FPApp) -> bool:
            app._live = object()  # noqa: SLF001 — 模拟在途流式
            fake.messages = [_msg("system", "SP"), _msg("user", "u1")]
            try:
                return app._sync_view()  # noqa: SLF001
            finally:
                app._live = None  # noqa: SLF001

        self.assertFalse(self.drive(initial, action)["value"])

    def test_unknown_role_is_drawn(self) -> None:
        """兜底渲染：未知 role 在视图里可见（否则视图 ≠ 上下文）"""
        initial = [_msg("system", "SP"), _msg("user", "u1"), _msg("assistant", "a1")]

        def action(fake: _StubPortal, app: FPApp) -> bool:
            # 末条被换成未知角色（同时构成断裂）——兜底卡必须出现
            fake.messages = [_msg("system", "SP"), _msg("user", "u1"), _msg("weird_role", "神秘内容")]
            redrawn = app._sync_view()  # noqa: SLF001
            out["texts"] = [str(child.render()) for child in app.query_one("#chat").children]
            return redrawn

        out: dict[str, Any] = {}
        self.assertTrue(self.drive(initial, action)["value"])
        self.assertTrue(any("weird_role" in t and "神秘内容" in t for t in out["texts"]), out["texts"])

    def test_redraw_is_capped(self) -> None:
        """超长投影只画尾部：出现折叠占位（长会话重建不拖垮终端）"""
        cap = FPApp.CTX_REDRAW_MAX_UNITS
        initial = [_msg("system", "SP"), _msg("user", "u1")]

        def action(fake: _StubPortal, app: FPApp) -> bool:
            fake.messages = [_msg("system", "SP")] + [_msg("assistant", f"m{i}") for i in range(cap + 5)]
            redrawn = app._sync_view()  # noqa: SLF001
            out["texts"] = [str(child.render()) for child in app.query_one("#chat").children]
            return redrawn

        out: dict[str, Any] = {}
        self.assertTrue(self.drive(initial, action)["value"])
        self.assertTrue(any("已折叠" in t for t in out["texts"]), out["texts"][:3])


if __name__ == "__main__":
    unittest.main()
