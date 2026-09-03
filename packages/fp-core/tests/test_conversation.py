"""测试 ConversationState — 上下文状态管理核心"""

from fp_core.core.conversation import ConversationState


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


class TestEdgeCases:
    """边界情况"""

    def test_system_at_position_0_only(self):
        """system prompt 始终在位置 0"""
        cs = ConversationState("system")
        assert cs[0]["role"] == "system"
        assert cs[0]["content"] == "system"

        cs.add_user_message("用户")
        assert cs[1]["role"] == "user"
        assert cs[1]["content"] == "用户"

    def test_empty_messages_for_llm(self):
        cs = ConversationState()
        assert cs.get_messages_for_llm() == []

    def test_single_system_message(self):
        cs = ConversationState("only system")
        msgs = cs.get_messages_for_llm()
        assert len(msgs) == 1
        assert msgs[0]["role"] == "system"


class TestReasoningContentForLLM:
    """get_messages_for_llm 的 reasoning_content 回传策略"""

    def test_with_tools_keeps_reasoning(self):
        from fp_core.core.conversation import ConversationState

        cs = ConversationState("sys")
        cs.add_user_message("q")
        cs._messages.append({
            "role": "assistant",
            "content": "a",
            "reasoning_content": "think",
            "tool_calls": [{"id": "1"}],
        })
        msgs = cs.get_messages_for_llm(with_tools=True)
        asst = [m for m in msgs if m["role"] == "assistant"][0]
        assert asst.get("reasoning_content") == "think"

    def test_without_tools_strips_reasoning(self):
        from fp_core.core.conversation import ConversationState

        cs = ConversationState("sys")
        cs.add_user_message("q")
        cs._messages.append({"role": "assistant", "content": "a", "reasoning_content": "think"})
        msgs = cs.get_messages_for_llm(with_tools=False)
        asst = [m for m in msgs if m["role"] == "assistant"][0]
        assert "reasoning_content" not in asst
        assert asst["content"] == "a"

    def test_default_keeps_reasoning(self):
        from fp_core.core.conversation import ConversationState

        cs = ConversationState("sys")
        cs._messages.append({"role": "assistant", "content": "a", "reasoning_content": "t"})
        msgs = cs.get_messages_for_llm()
        assert msgs[1].get("reasoning_content") == "t"
