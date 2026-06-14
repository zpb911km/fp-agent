"""测试 ConversationState — 上下文状态管理核心"""

import pytest

from fp_core.core.conversation import CompactConfig, ConversationState


class TestConversationInit:
    """ConversationState 初始化"""

    def test_empty_init(self):
        """无 system prompt 初始化 → 空消息列表"""
        cs = ConversationState()
        assert len(cs) == 0
        assert cs.messages == []

    def test_init_with_system_prompt(self):
        """带 system prompt 初始化 → 第一条是 system"""
        cs = ConversationState("你是助手")
        assert len(cs) == 1
        assert cs.messages[0]["role"] == "system"
        assert cs.messages[0]["content"] == "你是助手"

    def test_system_prompt_property(self):
        """system_prompt 属性返回第一条 system 内容"""
        cs = ConversationState("测试 prompt")
        assert cs.system_prompt == "测试 prompt"

    def test_system_prompt_when_empty(self):
        """无 system prompt 时返回空字符串"""
        cs = ConversationState()
        assert cs.system_prompt == ""


class TestConversationBasicOps:
    """基本增删改操作"""

    def test_append(self):
        cs = ConversationState()
        cs.append({"role": "user", "content": "你好"})
        assert len(cs) == 1
        assert cs[0]["content"] == "你好"

    def test_extend(self):
        cs = ConversationState()
        cs.extend([
            {"role": "user", "content": "A"},
            {"role": "assistant", "content": "B"},
        ])
        assert len(cs) == 2

    def test_insert(self):
        cs = ConversationState("system")
        cs.append({"role": "user", "content": "第二条"})
        cs.insert(1, {"role": "user", "content": "插入的"})
        assert cs[1]["content"] == "插入的"
        assert cs[2]["content"] == "第二条"

    def test_clear(self):
        cs = ConversationState("system")
        cs.append({"role": "user", "content": "数据"})
        cs.clear()
        assert len(cs) == 0

    def test_reset(self):
        cs = ConversationState("旧的 system")
        cs.append({"role": "user", "content": "旧消息"})
        cs.reset("新的 system")
        assert len(cs) == 1
        assert cs[0]["content"] == "新的 system"

    def test_replace_all(self):
        cs = ConversationState("system")
        cs.append({"role": "user", "content": "将被替换"})
        cs.replace_all([{"role": "user", "content": "新内容"}])
        assert len(cs) == 1
        assert cs[0]["content"] == "新内容"

    def test_messages_returns_copy(self):
        """messages 属性返回防御性拷贝，修改不影响内部"""
        cs = ConversationState()
        cs.append({"role": "user", "content": "test"})
        external = cs.messages
        external.append({"role": "user", "content": "hack"})
        assert len(cs) == 1  # 内部未被修改


class TestAddMethods:
    """便捷添加方法"""

    def test_add_user_message(self):
        cs = ConversationState()
        result = cs.add_user_message("用户消息")
        assert result["role"] == "user"
        assert result["content"] == "用户消息"
        assert len(cs) == 1

    def test_add_assistant_message(self):
        cs = ConversationState()
        result = cs.add_assistant_message({"role": "assistant", "content": "回复"})
        assert result["role"] == "assistant"
        assert result["content"] == "回复"

    def test_add_assistant_does_not_mutate_input(self):
        """add_assistant_message 不修改传入的 dict"""
        original = {"role": "assistant", "content": "回复", "extra": "data"}
        cs = ConversationState()
        cs.add_assistant_message(original)
        assert "extra" in original  # 原 dict 不受影响

    def test_add_tool_message(self):
        cs = ConversationState()
        result = cs.add_tool_message("call_123", "工具返回结果")
        assert result["role"] == "tool"
        assert result["tool_call_id"] == "call_123"
        assert result["content"] == "工具返回结果"

    def test_add_system_message(self):
        cs = ConversationState()
        result = cs.add_system_message("系统消息")
        assert result["role"] == "system"
        assert result["content"] == "系统消息"


class TestQueryMethods:
    """查询方法"""

    def test_get_non_system_messages(self):
        cs = ConversationState("system")
        cs.add_user_message("用户")
        cs.add_assistant_message({"role": "assistant", "content": "AI"})
        non_system = cs.get_non_system_messages()
        assert len(non_system) == 2
        assert all(m["role"] != "system" for m in non_system)

    def test_get_non_system_count(self):
        cs = ConversationState("system")
        cs.add_user_message("A")
        cs.add_assistant_message({"role": "assistant", "content": "B"})
        assert cs.get_non_system_count() == 2

    def test_get_messages_for_llm_ordering(self):
        """get_messages_for_llm 保持 system 在第一条"""
        cs = ConversationState("system prompt")
        cs.add_user_message("用户")
        cs.add_assistant_message({"role": "assistant", "content": "AI"})
        msgs = cs.get_messages_for_llm()
        assert msgs[0]["role"] == "system"
        assert msgs[0]["content"] == "system prompt"

    def test_get_last_assistant_message(self):
        cs = ConversationState()
        cs.add_user_message("用户")
        cs.add_assistant_message({"role": "assistant", "content": "回复1"})
        cs.add_user_message("追问")
        cs.add_assistant_message({"role": "assistant", "content": "回复2"})
        last = cs.get_last_assistant_message()
        assert last is not None
        assert last["content"] == "回复2"

    def test_get_last_assistant_message_none(self):
        cs = ConversationState()
        assert cs.get_last_assistant_message() is None

    def test_get_last_content(self):
        cs = ConversationState()
        cs.add_assistant_message({"role": "assistant", "content": "最终回复"})
        assert cs.get_last_content() == "最终回复"

    def test_get_last_content_empty(self):
        cs = ConversationState()
        assert cs.get_last_content() == ""

    def test_len(self):
        cs = ConversationState("system")
        cs.add_user_message("1")
        assert len(cs) == 2

    def test_set_system_prompt_replace(self):
        """set_system_prompt 替换已有的 system prompt"""
        cs = ConversationState("旧的")
        cs.set_system_prompt("新的")
        assert cs.system_prompt == "新的"

    def test_set_system_prompt_when_empty(self):
        """无 system prompt 时 set_system_prompt 插入"""
        cs = ConversationState()
        cs.set_system_prompt("插入的")
        assert cs[0]["content"] == "插入的"
        assert cs[0]["role"] == "system"


class TestToolOrdering:
    """_repair_ordering() — 工具消息顺序修复"""

    def test_normal_ordering_unchanged(self):
        """正常 assistant+tools 顺序不应该被修改"""
        msgs = [
            {"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "type": "function"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "结果"},
        ]
        result = ConversationState._repair_ordering(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "assistant"
        assert result[1]["role"] == "tool"

    def test_orphan_tool_converted_to_system(self):
        """孤儿 tool（无对应 tool_calls）→ 转为 system 消息"""
        msgs = [
            {"role": "assistant", "content": "正常回复"},
            {"role": "tool", "tool_call_id": "orphan", "content": "无家可归的结果"},
        ]
        result = ConversationState._repair_ordering(msgs)
        assert len(result) == 2
        assert result[0]["role"] == "assistant"
        assert result[1]["role"] == "system"
        assert "无家可归的结果" in result[1]["content"]

    def test_multiple_tools(self):
        """多个 tool_call → tool_result 配对"""
        msgs = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "c1"},
                    {"id": "c2"},
                    {"id": "c3"},
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "结果1"},
            {"role": "tool", "tool_call_id": "c2", "content": "结果2"},
            {"role": "tool", "tool_call_id": "c3", "content": "结果3"},
        ]
        result = ConversationState._repair_ordering(msgs)
        assert len(result) == 4

    def test_interleaved_orphans(self):
        """正常消息和孤儿 tool 交错"""
        msgs = [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "回复", "tool_calls": [{"id": "c1"}]},
            {"role": "tool", "tool_call_id": "c1", "content": "结果"},
            {"role": "assistant", "content": "最终回复"},
            {"role": "tool", "tool_call_id": "orphan", "content": "被遗漏的结果"},
        ]
        result = ConversationState._repair_ordering(msgs)
        # 最后一组孤儿 tool 合并为 system 消息
        assert any(m["role"] == "system" and "被遗漏的结果" in m["content"] for m in result)


class TestBack:
    """回退功能"""

    def test_back_normal(self):
        cs = ConversationState("system")
        cs.add_user_message("A")
        cs.add_assistant_message({"role": "assistant", "content": "B"})
        cs.add_user_message("C")

        deleted = cs.back(target_idx=2, mode=2)
        assert deleted > 0
        assert len(cs) == 3  # system + A + B

    def test_back_invalid_index(self):
        cs = ConversationState("system")
        cs.add_user_message("A")
        deleted = cs.back(target_idx=99, mode=2)
        assert deleted == 0

    def test_back_no_history(self):
        cs = ConversationState("system")
        deleted = cs.back(target_idx=1, mode=2)
        assert deleted == 0

    def test_back_mode_1_retains(self):
        """mode=1 保留后续消息"""
        cs = ConversationState("system")
        cs.add_user_message("A")
        cs.add_assistant_message({"role": "assistant", "content": "B"})
        deleted = cs.back(target_idx=1, mode=1)
        assert deleted == 0  # 未删除
        assert len(cs) == 3


class TestForkSnapshot:
    """分支快照"""

    def test_fork_snapshot_contains_all(self):
        cs = ConversationState("system")
        cs.add_user_message("A")
        cs.add_assistant_message({"content": "B"})
        snapshot = cs.fork_snapshot()
        assert len(snapshot) == 3
        assert snapshot[0]["role"] == "system"

    def test_fork_snapshot_is_copy(self):
        """快照是拷贝，修改快照不影响原状态"""
        cs = ConversationState("system")
        snapshot = cs.fork_snapshot()
        snapshot.append({"role": "user", "content": "hack"})
        assert len(cs) == 1


class TestCompact:
    """压缩功能"""

    @pytest.mark.asyncio
    async def test_compact_no_summarizer_raises(self):
        """summarizer 为 None 时抛出 ValueError"""
        cs = ConversationState("system")
        cs.add_user_message("A")
        cs.add_assistant_message({"role": "assistant", "content": "B"})
        with pytest.raises(ValueError, match="summarizer"):
            await cs.compact(None)

    @pytest.mark.asyncio
    async def test_compact_short_history(self):
        """历史太短时不压缩"""
        cs = ConversationState("system")
        cs.add_user_message("A")

        async def fake_summarizer(_):
            return "摘要"

        result, desc = await cs.compact(fake_summarizer, CompactConfig(keep_meaningful=4))
        assert result is False
        assert "无需压缩" in desc

    @pytest.mark.asyncio
    async def test_compact_success(self):
        """成功压缩"""
        cs = ConversationState("system")
        # 添加 6 条消息，超过 keep_meaningful=4
        for i in range(6):
            cs.add_user_message(f"消息{i}")
            cs.add_assistant_message({"role": "assistant", "content": f"回复{i}"})

        async def fake_summarizer(text: str) -> str:
            return "这是前几条消息的摘要"

        result, desc = await cs.compact(fake_summarizer, CompactConfig(keep_meaningful=4))
        assert result is True
        assert "已压缩" in desc
        # 摘要后总消息数 = system + 摘要 + 保留的
        assert len(cs) < 13  # 原始 1 + 12

    @pytest.mark.asyncio
    async def test_compact_summarizer_error(self):
        """summarizer 抛出异常 → 不崩溃，返回错误"""
        cs = ConversationState("system")
        for i in range(6):
            cs.add_user_message(f"消息{i}")
            cs.add_assistant_message({"role": "assistant", "content": f"回复{i}"})

        async def broken_summarizer(_):
            raise RuntimeError("模拟失败")

        result, desc = await cs.compact(broken_summarizer)
        assert result is False
        assert "失败" in desc


class TestEdgeCases:
    """边界情况"""

    def test_system_at_position_0_only(self):
        """system prompt 始终在位置 0"""
        cs = ConversationState("system")
        cs.add_system_message("另一个 system")
        assert cs[0]["role"] == "system"
        assert cs[0]["content"] == "system"  # 没被替换
        assert cs[1]["role"] == "system"
        assert cs[1]["content"] == "另一个 system"

    def test_empty_messages_for_llm(self):
        cs = ConversationState()
        assert cs.get_messages_for_llm() == []

    def test_single_system_message(self):
        cs = ConversationState("only system")
        msgs = cs.get_messages_for_llm()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system"
