"""测试 shortcircuit 命令/工具 — 连通块扫描、退化（degenerate）、合并（crop/regenerate）"""

# 测试需要直接验证私有实现函数，关闭私有符号告警
# pyright: reportPrivateUsage=false

from collections.abc import Coroutine

from fp_core.commands.shortcircuit import (
    Message,
    _degenerate,
    _parse_args,
    _scan_components,
    _shortcircuit,
)


def _msg(
    role: str,
    content: str = "",
    tool_calls: list[dict[str, object]] | None = None,
    tool_call_id: str = "t1",
) -> Message:
    m: Message = {"role": role}
    if content:
        m["content"] = content
    if tool_calls is not None:
        m["tool_calls"] = tool_calls
    if role == "tool":
        m["tool_call_id"] = tool_call_id
    return m


def _tc(name: str) -> list[dict[str, object]]:
    return [{"id": f"call_{name}", "type": "function", "function": {"name": name, "arguments": "{}"}}]


# ═══════════════════════════════════════════════════════
# _scan_components
# ═══════════════════════════════════════════════════════


class TestScanComponents:
    def test_basic_user_split(self):
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "回复A"),
            _msg("user", "任务B"),
            _msg("assistant", "回复B"),
        ]
        comps = _scan_components(messages)
        assert len(comps) == 2
        assert comps[0]["idx"] == 1 and comps[0]["message_count"] == 2
        assert comps[1]["idx"] == 2 and comps[1]["message_count"] == 2

    def test_degenerable_flag(self):
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "", tool_calls=_tc("s1")),
            _msg("tool", "结果1"),
            _msg("assistant", "总结"),
        ]
        comps = _scan_components(messages)
        assert comps[0]["degenerable"] is True
        assert comps[0]["compressible"] is True
        assert comps[0]["complete"] is True

    def test_degenerable_false_when_plain(self):
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "回复"),
        ]
        comps = _scan_components(messages)
        assert comps[0]["degenerable"] is False
        assert comps[0]["compressible"] is False

    def test_incomplete_marked(self):
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "", tool_calls=_tc("s1")),
        ]
        comps = _scan_components(messages)
        assert comps[0]["complete"] is False
        assert comps[0]["degenerable"] is True


# ═══════════════════════════════════════════════════════
# _degenerate
# ═══════════════════════════════════════════════════════


class TestDegenerate:
    def test_removes_tool_chain_keeps_text(self):
        """基本退化：工具调用链 → 纯文本 assistant 链"""
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "开始做", tool_calls=_tc("s1")),
            _msg("tool", "结果1"),
            _msg("assistant", "第1步完成"),
        ]
        ok, _, changed, new = _degenerate(messages, [(0, 3)], protect_callsite=False)
        assert ok
        assert new is not None
        assert changed == 2  # 1 条 tool 删除 + 1 条 assistant 转正
        roles = [m["role"] for m in new]
        assert roles == ["user", "assistant", "assistant"]
        # 有 content 的 tool_calls assistant 转正：保留 content、删除 tool_calls
        assert new[1]["content"] == "开始做"
        assert "tool_calls" not in new[1]
        assert new[2]["content"] == "第1步完成"

    def test_empty_content_toolcalls_removed(self):
        """只有 tool_calls 没有 content 的 assistant → 整条删除"""
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "", tool_calls=_tc("s1")),
            _msg("tool", "结果1"),
            _msg("assistant", "完成"),
        ]
        ok, _, changed, new = _degenerate(messages, [(0, 3)], protect_callsite=False)
        assert ok
        assert new is not None
        assert changed == 2  # 空 content assistant + tool 删除
        assert len(new) == 2
        assert new[0]["role"] == "user"
        assert new[1]["role"] == "assistant"
        assert new[1]["content"] == "完成"

    def test_no_tool_noise_no_change(self):
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "回复"),
        ]
        ok, _, changed, new = _degenerate(messages, [(0, 1)], protect_callsite=False)
        assert ok
        assert new is not None
        assert changed == 0
        assert new == messages

    def test_protect_callsite_keeps_calling_point(self):
        """工具情境：进行中块的调用点（最后一条 assistant+tool_calls）本轮不动，
        其前的纯文本 assistant 逆序并入调用点 content（多条合并一条）"""
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "开始", tool_calls=_tc("s1")),
            _msg("tool", "结果1"),
            _msg("assistant", "第1步完成", tool_calls=_tc("shortcircuit")),  # 调用点
        ]
        ok, _, changed, new = _degenerate(messages, [(0, 3)], protect_callsite=True)
        assert ok
        assert new is not None
        assert changed == 2  # s1 的 assistant 转正 + tool 删除
        # 调用点原样保留（含 tool_calls），其前的纯文本 assistant 已逆序并入调用点 content
        assert len(new) == 2  # user + 调用点（合并后不残留独立 assistant）
        assert new[-1]["role"] == "assistant"
        assert new[-1]["tool_calls"][0]["function"]["name"] == "shortcircuit"
        # 调用点 content = 前文文本（时间在前） + 调用点原文（时间在后）
        assert new[-1]["content"] == "开始\n第1步完成"

    def test_protect_callsite_only_block_of_user_and_callsite(self):
        """块内只有 user + 调用点 → 无可退化内容，整块原样保留"""
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "回复中", tool_calls=_tc("shortcircuit")),
        ]
        ok, _, changed, new = _degenerate(messages, [(0, 1)], protect_callsite=True)
        assert ok
        assert new is not None
        assert changed == 0
        assert len(new) == 2
        assert new[1]["tool_calls"][0]["function"]["name"] == "shortcircuit"

    def test_degenerate_merges_consecutive_assistant_into_callsite(self):
        """退化哲学：受保护调用点前的连续纯文本 assistant 逆序并入调用点 content
        （多条合并一条，消除连续 assistant 结构）。"""
        cases: list[tuple[list[Message], str]] = [
            # 调用点前是纯文本 assistant（阶段回复）
            (
                [
                    _msg("user", "任务"),
                    _msg("assistant", "第1步开始", tool_calls=_tc("s1")),
                    _msg("tool", "T1"),
                    _msg("assistant", "阶段总结", tool_calls=_tc("sc")),
                ],
                "第1步开始\n阶段总结",
            ),
            # 调用点前有多个纯文本 assistant（多步转正，逆序保持时间顺序）
            (
                [
                    _msg("user", "任务"),
                    _msg("assistant", "第1步", tool_calls=_tc("s1")),
                    _msg("tool", "T1"),
                    _msg("assistant", "第2步", tool_calls=_tc("s2")),
                    _msg("tool", "T2"),
                    _msg("assistant", "总结", tool_calls=_tc("sc")),
                ],
                "第1步\n第2步\n总结",
            ),
            # 连续两次 sc：上次调用点残骸（转正文本 + tool）在退化块内被清理
            (
                [
                    _msg("user", "任务"),
                    _msg("assistant", "第1步文本", tool_calls=_tc("sc1")),
                    _msg("tool", "上次sc结果"),
                    _msg("assistant", "第2步总结", tool_calls=_tc("sc")),
                ],
                "第1步文本\n第2步总结",
            ),
        ]
        for messages, expected_content in cases:
            ok, _, _, new = _degenerate(messages, [(0, len(messages) - 1)], protect_callsite=True)
            assert ok
            assert new is not None
            # 调用点（最后一条带 tool_calls）本身保留 tool_calls
            assert new[-1]["role"] == "assistant"
            assert new[-1]["tool_calls"][0]["function"]["name"] == "sc"
            # 多条合并一条：调用点前不残留纯文本 assistant（只有 user 在它前面）
            assert len(new) == 2, f"应合并为 user + 调用点: {new}"
            assert new[0]["role"] == "user"
            # 调用点 content = 前文文本（时间在前） + 调用点原文（时间在后）
            assert new[-1]["content"] == expected_content, f"期望 {expected_content!r}, 实际 {new[-1]['content']!r}"
            # 所有工具噪音已清理
            assert not any(m["role"] == "tool" for m in new)

    def test_three_step_merge_into_callsite(self):
        """核心场景：三步任务连续退化，前文文本逆序全部合并进调用点 content
        （多条合并一条），无信息丢失、无连续 assistant 结构"""
        # 第1步
        msgs: list[Message] = [
            _msg("user", "分3步做X"),
            _msg("assistant", "第一步开始", tool_calls=_tc("step1")),
            _msg("tool", "T1"),
            _msg("assistant", "第1步完成：结果A", tool_calls=_tc("shortcircuit")),
        ]
        ok, _, _, result = _degenerate(msgs, [(0, 3)], protect_callsite=True)
        assert ok
        assert result is not None
        msgs = result
        # 追加 sc 的 tool 结果 + 第2步
        msgs.append(_msg("tool", "sc已处理"))
        msgs.append(_msg("assistant", "第二步开始", tool_calls=_tc("step2")))
        msgs.append(_msg("tool", "T2"))
        msgs.append(_msg("assistant", "第2步完成：结果B", tool_calls=_tc("shortcircuit")))
        # 第2次退化：第1步文本合并进新的调用点 content，不丢失
        ok, _, _, result = _degenerate(msgs, [(0, len(msgs) - 1)], protect_callsite=True)
        assert ok
        assert result is not None
        msgs = result
        assert msgs[-1]["role"] == "assistant"
        assert msgs[-1]["tool_calls"][0]["function"]["name"] == "shortcircuit"
        assert "第1步完成：结果A" in msgs[-1]["content"]  # 第1步结果未被吞
        assert "第2步完成：结果B" in msgs[-1]["content"]  # 第2步结果在调用点原文
        # 多条合并一条：调用点前只残留 user，无独立纯文本 assistant
        assert len(msgs) == 2 and msgs[0]["role"] == "user"
        # 第2步的工具噪音（step2/T2/sc 结果）已清理
        assert not any(m["role"] == "tool" for m in msgs)
        # 第3步：继续退化，第1/2步文本仍保留（合并进最终调用点 content）
        step3_start = msgs + [
            _msg("assistant", "第三步开始", tool_calls=_tc("step3")),
            _msg("tool", "T3"),
            _msg("assistant", "第3步完成：结果C", tool_calls=_tc("shortcircuit")),
        ]
        ok, _, _, final = _degenerate(step3_start, [(0, len(step3_start) - 1)], protect_callsite=True)
        assert ok
        assert final is not None
        assert len(final) == 2  # user + 调用点（所有文本合并）
        assert "第1步完成：结果A" in final[1]["content"]
        assert "第2步完成：结果B" in final[1]["content"]
        assert "第三步开始" in final[1]["content"]
        assert "第3步完成：结果C" in final[1]["content"]
        # 文本按时间顺序排列（最早在前，调用点原文最后）
        c = final[1]["content"]
        assert c.index("第1步完成：结果A") < c.index("第2步完成：结果B") < c.index("第3步完成：结果C")

    def test_multi_target(self):
        """多个目标块分别退化（对应 block_ids / range 选择）"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("a")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _msg("assistant", "B完成"),
        ]
        ok, _, changed, new = _degenerate(messages, [(0, 3), (4, 7)], protect_callsite=False)
        assert ok
        assert new is not None
        assert changed == 4  # 两个块的 tool 删除 + 两个 assistant 转正
        assert not any(m["role"] == "tool" for m in new)
        assert len([m for m in new if m["role"] == "assistant"]) == 4


# ═══════════════════════════════════════════════════════
# _shortcircuit（合并模式照旧）
# ═══════════════════════════════════════════════════════


class TestShortcircuitMerge:
    def test_crop_merge_unchanged_behavior(self):
        """crop 合并：历史完整块压缩为 user + terminal 两条"""
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "", tool_calls=_tc("s1")),
            _msg("tool", "结果1"),
            _msg("assistant", "总结"),
        ]
        ok, _, saved, new = await_result(_shortcircuit(messages, None, [(0, 3)], "crop"))
        assert ok
        assert new is not None
        assert saved == 2
        assert len(new) == 2
        assert new[0]["content"] == "任务"
        assert new[1]["content"] == "总结"

    def test_range_merge_to_single_block(self):
        """range 合并：多个连通块合并为一个 user + 最后 terminal"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B完成"),
        ]
        ok, _, _, new = await_result(_shortcircuit(messages, None, [(0, 3)], "crop"))
        assert ok
        assert new is not None
        assert len(new) == 2
        assert new[0]["content"] == "任务A"
        assert new[1]["content"] == "B完成"


# ═══════════════════════════════════════════════════════
# _parse_args
# ═══════════════════════════════════════════════════════


class TestParseArgs:
    def test_default_crop(self):
        assert _parse_args("") == ("default", 1, "crop")

    def test_d_flag(self):
        assert _parse_args("-d") == ("default", 1, "degenerate")
        assert _parse_args("#2 -d") == ("index", 2, "degenerate")
        assert _parse_args("3 -d") == ("count", 3, "degenerate")
        assert _parse_args("#1-#3 -d") == ("range", (1, 3), "degenerate")

    def test_mode_flags_override(self):
        # 最后一个模式修饰生效
        assert _parse_args("-d -c") == ("default", 1, "crop")
        assert _parse_args("-r") == ("default", 1, "regenerate")

    def test_list(self):
        assert _parse_args("list") == ("list", None, "crop")
        assert _parse_args("list -d") == ("list", None, "degenerate")


def await_result(
    coro: Coroutine[None, None, tuple[bool, str, int, list[Message] | None]],
) -> tuple[bool, str, int, list[Message] | None]:
    import asyncio

    return asyncio.run(coro)
