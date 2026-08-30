"""测试 shortcircuit 插件（core.py）— 连通块扫描、退化（degenerate）、合并（crop/regenerate）"""

from collections.abc import Coroutine

from fp_core.plugins.shortcircuit.core import (
    Message,
    degenerate,
    execute,
    execute_plan,
    parse_args,
    scan_components,
    shortcircuit,
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
        comps = scan_components(messages)
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
        comps = scan_components(messages)
        assert comps[0]["degenerable"] is True
        assert comps[0]["compressible"] is True
        assert comps[0]["complete"] is True

    def test_degenerable_false_when_plain(self):
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "回复"),
        ]
        comps = scan_components(messages)
        assert comps[0]["degenerable"] is False
        assert comps[0]["compressible"] is False

    def test_incomplete_marked(self):
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "", tool_calls=_tc("s1")),
        ]
        comps = scan_components(messages)
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
        ok, _, changed, new = degenerate(messages, [(0, 3)], protect_callsite=False)
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
        ok, _, changed, new = degenerate(messages, [(0, 3)], protect_callsite=False)
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
        ok, _, changed, new = degenerate(messages, [(0, 1)], protect_callsite=False)
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
        ok, _, changed, new = degenerate(messages, [(0, 3)], protect_callsite=True)
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
        ok, _, changed, new = degenerate(messages, [(0, 1)], protect_callsite=True)
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
            ok, _, _, new = degenerate(messages, [(0, len(messages) - 1)], protect_callsite=True)
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
        ok, _, _, result = degenerate(msgs, [(0, 3)], protect_callsite=True)
        assert ok
        assert result is not None
        msgs = result
        # 追加 sc 的 tool 结果 + 第2步
        msgs.append(_msg("tool", "sc已处理"))
        msgs.append(_msg("assistant", "第二步开始", tool_calls=_tc("step2")))
        msgs.append(_msg("tool", "T2"))
        msgs.append(_msg("assistant", "第2步完成：结果B", tool_calls=_tc("shortcircuit")))
        # 第2次退化：第1步文本合并进新的调用点 content，不丢失
        ok, _, _, result = degenerate(msgs, [(0, len(msgs) - 1)], protect_callsite=True)
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
        ok, _, _, final = degenerate(step3_start, [(0, len(step3_start) - 1)], protect_callsite=True)
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
        ok, _, changed, new = degenerate(messages, [(0, 3), (4, 7)], protect_callsite=False)
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
        ok, _, saved, new = await_result(shortcircuit(messages, None, [(0, 3)], "crop"))
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
        ok, _, _, new = await_result(shortcircuit(messages, None, [(0, 3)], "crop"))
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
        assert parse_args("") == ("default", 1, "crop")

    def test_d_flag(self):
        assert parse_args("-d") == ("default", 1, "degenerate")
        assert parse_args("#2 -d") == ("index", 2, "degenerate")
        assert parse_args("3 -d") == ("count", 3, "degenerate")
        assert parse_args("#1-#3 -d") == ("range", (1, 3), "degenerate")

    def test_mode_flags_override(self):
        # 最后一个模式修饰生效
        assert parse_args("-d -c") == ("default", 1, "crop")
        assert parse_args("-r") == ("default", 1, "regenerate")

    def test_list(self):
        assert parse_args("list") == ("list", None, "crop")
        assert parse_args("list -d") == ("list", None, "degenerate")


# ═══════════════════════════════════════════════════════
# execute_plan（统一策略：当前块只能 -d，其他块默认 crop）
# ═══════════════════════════════════════════════════════


class TestExecutePlan:
    def test_mixed_others_crop_currentdegenerate(self):
        """工具层典型场景（count=2, mode=None）：历史块默认 crop，
        当前块（含调用点）强制退化并保护调用点"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("a")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _msg("assistant", "B完成", tool_calls=_tc("shortcircuit")),  # 调用点
        ]
        ok, msg, saved, new = await_result(execute_plan(messages, None, "count", 2, None, "degenerable", True))
        assert ok
        assert "裁剪" in msg and "退化当前块" in msg
        assert new is not None
        assert saved == 4  # #1 crop 省 2 + #2 退化清 2
        # #1 被 crop：压缩为 user + terminal 两条
        assert new[0]["content"] == "任务A"
        assert new[1]["content"] == "A完成"
        # #2 被退化且保护调用点：调用点保留 tool_calls，其前文本合并进 content
        assert len(new) == 4
        assert new[2]["role"] == "user" and new[2]["content"] == "任务B"
        assert new[3]["role"] == "assistant"
        assert new[3]["tool_calls"][0]["function"]["name"] == "shortcircuit"
        assert new[3]["content"] == "B开始\nB完成"
        assert not any(m["role"] == "tool" for m in new)

    def test_current_forced_degenerate_even_if_crop(self):
        """当前块强制 -d：显式 crop 指定当前块 → 实际执行退化（保留 AI 文本）"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _msg("assistant", "B完成"),
        ]
        ok, msg, _, new = await_result(execute_plan(messages, None, "index", 2, "crop", "compressible", False))
        assert ok
        assert "退化当前块" in msg and "裁剪" not in msg
        assert new is not None
        # 退化而非 crop：B开始 转正为独立 assistant（crop 则会压缩为 2 条）
        assert [m["role"] for m in new] == ["user", "assistant", "user", "assistant", "assistant"]
        assert new[3]["content"] == "B开始"
        assert new[4]["content"] == "B完成"
        assert not any(m["role"] == "tool" for m in new)

    def test_others_default_crop_when_mode_none(self):
        """其他块默认 crop：未指定 mode 处理历史块 → 压缩而非退化"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("a")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B完成"),
        ]
        ok, msg, saved, new = await_result(execute_plan(messages, None, "index", 1, None, "compressible", False))
        assert ok
        assert "裁剪" in msg
        assert new is not None
        assert saved == 2
        # #1 被 crop（压缩为2条），#2 原样保留
        assert len(new) == 4
        assert new[0]["content"] == "任务A"
        assert new[1]["content"] == "A完成"
        assert new[2]["content"] == "任务B"
        assert new[3]["content"] == "B完成"

    def test_range_merges_others_currentdegenerate(self):
        """范围指定涉及当前块：其他块按指定行为（crop 合并），当前块强制 -d"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B完成"),
            _msg("user", "任务C"),
            _msg("assistant", "C开始", tool_calls=_tc("c")),
            _msg("tool", "C结果"),
            _msg("assistant", "C总结", tool_calls=_tc("shortcircuit")),
        ]
        ok, msg, _, new = await_result(execute_plan(messages, None, "range", (1, 3), "crop", "compressible", True))
        assert ok
        assert "裁剪 2 个连通块" in msg and "退化当前块" in msg
        assert new is not None
        # #1+#2 按 range 合并为一个连通块（user 取首、assistant 取末）
        assert len(new) == 4
        assert new[0]["content"] == "任务A"
        assert new[1]["content"] == "B完成"
        # #3 退化且保护调用点
        assert new[2]["content"] == "任务C"
        assert new[3]["role"] == "assistant"
        assert new[3]["tool_calls"][0]["function"]["name"] == "shortcircuit"
        assert new[3]["content"] == "C开始\nC总结"
        assert not any(m["role"] == "tool" for m in new)

    def test_all_degenerate_when_mode_explicit(self):
        """显式 -d 且目标含当前块：全部退化（其他块也按指定行为 -d），保持原全退化语义"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("a")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _msg("assistant", "B完成", tool_calls=_tc("shortcircuit")),
        ]
        ok, msg, _, new = await_result(execute_plan(messages, None, "count", 2, "degenerate", "degenerable", True))
        assert ok
        assert "退化" in msg
        assert new is not None
        # #1 退化保留转正文本（非 crop 压缩）；#2 调用点保护 + 文本合并
        assert new[0]["content"] == "任务A"
        assert new[1]["content"] == "A开始"
        assert new[2]["content"] == "A完成"
        assert new[3]["content"] == "任务B"
        assert new[4]["role"] == "assistant"
        assert new[4]["tool_calls"][0]["function"]["name"] == "shortcircuit"
        assert new[4]["content"] == "B开始\nB完成"
        assert not any(m["role"] == "tool" for m in new)

    def test_default_targets_current_block(self):
        """默认（未指定 mode/编号）：目标为最晚可退化块（当前块）→ 强制 -d"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _msg("assistant", "B完成"),
        ]
        ok, msg, _, new = await_result(execute_plan(messages, None, "default", None, None, "degenerable", True))
        assert ok
        assert "退化当前块" in msg
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant", "user", "assistant", "assistant"]
        assert new[3]["content"] == "B开始"
        assert not any(m["role"] == "tool" for m in new)


# ═══════════════════════════════════════════════════════
# execute（命令层 /sc —— 无硬性约束，可自由处理任意块含当前块）
# ═══════════════════════════════════════════════════════


class _FakeConversation:
    def __init__(self, messages: list[Message]) -> None:
        self._msgs = list(messages)
        self.system_prompt = {"role": "system", "content": "sys"}

    def get_non_system_messages(self) -> list[Message]:
        return list(self._msgs)

    def set_messages(self, system_prompt: object, messages: list[Message]) -> None:
        self._msgs = list(messages)

    def to_serializable(self) -> dict[str, object]:
        return {"messages": self._msgs}


class _FakeSession:
    def __init__(self) -> None:
        self.saved: list[dict[str, object]] = []

    def save_context(self, data: dict[str, object]) -> None:
        self.saved.append(data)


class _FakeState:
    def __init__(self, messages: list[Message]) -> None:
        self.conversation = _FakeConversation(messages)
        self.session = _FakeSession()


class TestExecuteCommand:
    def test_command_can_crop_current_block(self):
        """命令层无硬性约束：/sc #N -c 可以 crop 当前块（不强制退化）"""
        st = _FakeState([
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _msg("assistant", "B完成"),
        ])
        ok, text = run_cmd(st, "#2 -c")
        assert ok
        assert "已处理" in text
        msgs = st.conversation._msgs
        # 当前块 #2 被 crop：压缩为 2 条（而非强制退化保留文本）
        assert len(msgs) == 4
        assert msgs[2]["content"] == "任务B"
        assert msgs[3]["content"] == "B完成"
        assert not any(m["role"] == "tool" for m in msgs)

    def test_command_range_merges_current_block(self):
        """命令层无硬性约束：/sc #1-#3 默认 crop 合并含当前块"""
        st = _FakeState([
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B完成"),
            _msg("user", "任务C"),
            _msg("assistant", "C开始", tool_calls=_tc("c")),
            _msg("tool", "C结果"),
            _msg("assistant", "C总结"),
        ])
        ok, text = run_cmd(st, "#1-#3")
        assert ok
        assert "已处理" in text
        msgs = st.conversation._msgs
        # 整个范围合并为一个块（range 语义），当前块不强制退化
        assert len(msgs) == 2
        assert msgs[0]["content"] == "任务A"
        assert msgs[1]["content"] == "C总结"

    def test_command_default_crop_on_current(self):
        """命令层无硬性约束：默认（无修饰）crop 最近可压缩块——即使它是当前块"""
        st = _FakeState([
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _msg("assistant", "B完成"),
        ])
        ok, text = run_cmd(st, "")
        assert ok
        assert "已处理" in text
        msgs = st.conversation._msgs
        assert len(msgs) == 4
        assert msgs[2]["content"] == "任务B"
        assert msgs[3]["content"] == "B完成"
        assert not any(m["role"] == "tool" for m in msgs)

    def test_command_explicit_degenerate_current(self):
        """命令层显式 -d 处理当前块：正常退化（本就允许）"""
        st = _FakeState([
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _msg("assistant", "B完成"),
        ])
        ok, text = run_cmd(st, "#2 -d")
        assert ok
        assert "已退化" in text
        msgs = st.conversation._msgs
        assert [m["role"] for m in msgs] == ["user", "assistant", "user", "assistant", "assistant"]
        assert msgs[3]["content"] == "B开始"
        assert not any(m["role"] == "tool" for m in msgs)


def run_cmd(state: _FakeState, arg: str) -> tuple[bool, str]:
    import asyncio

    return asyncio.run(execute(state, arg))


def await_result(
    coro: Coroutine[None, None, tuple[bool, str, int, list[Message] | None]],
) -> tuple[bool, str, int, list[Message] | None]:
    import asyncio

    return asyncio.run(coro)
