"""
ConversationState — 上下文状态的唯一所有者

职责：
- 持有对话消息列表 _messages（含 system prompt）
- 提供所有消息操作：添加、删除、回退、fork、修复、压缩
- 不处理 IO、不处理持久化、不处理 LLM 调用
- 不持有 SessionManager 引用

所有权清晰：
  ConversationState._messages  ← 唯一的事实源
  Agent 通过 ConvState 操作消息
  SessionStore 只做持久化（不持有状态）
"""


class ConversationState:
    """上下文状态的唯一所有者"""

    def __init__(self, system_prompt: str = ""):
        self._messages: list[dict] = []
        if system_prompt:
            self._messages.append({"role": "system", "content": system_prompt})

    # ── 只读属性 ─────────────────────────────────────

    @property
    def messages(self) -> list[dict]:
        """返回消息列表的防御性拷贝"""
        return list(self._messages)

    @property
    def system_prompt(self) -> str:
        if self._messages and self._messages[0].get("role") == "system":
            return self._messages[0].get("content", "")
        return ""

    def __len__(self) -> int:
        return len(self._messages)

    def __getitem__(self, idx: int) -> dict:
        return self._messages[idx]

    # ── 写操作 ───────────────────────────────────────

    def set_system_prompt(self, prompt: str):
        """设置或替换 system prompt（始终在第一条）"""
        if self._messages and self._messages[0].get("role") == "system":
            self._messages[0]["content"] = prompt
        else:
            self._messages.insert(0, {"role": "system", "content": prompt})

    def set_messages(self, system_prompt: str, non_system_messages: list[dict]):
        """「大通道」批量替换 — 一次性替换全部消息

        fork/back/switch 等需要批量操作的场景使用此方法，
        避免调用方直接操作私有属性 _messages。

        Args:
            system_prompt: 新的 system prompt
            non_system_messages: 非 system 消息列表（每条 dict，会被拷贝）
        """
        self._messages = [{"role": "system", "content": system_prompt}] + [dict(m) for m in non_system_messages]

    def append(self, message: dict):
        """追加一条消息"""
        self._messages.append(message)

    def extend(self, messages: list[dict]):
        """批量追加消息"""
        self._messages.extend(messages)

    def replace_all(self, messages: list[dict]):
        """替换整个消息列表"""
        self._messages = list(messages)

    def insert(self, index: int, message: dict):
        """在指定位置插入消息"""
        self._messages.insert(index, message)

    def clear(self):
        """清空所有消息"""
        self._messages.clear()

    def reset(self, system_prompt: str):
        """重置为只有 system prompt"""
        self._messages.clear()
        self._messages.append({"role": "system", "content": system_prompt})

    # ── 便捷添加方法 ─────────────────────────────────

    def add_user_message(self, content: str) -> dict:
        msg = {"role": "user", "content": content}
        self._messages.append(msg)
        return msg

    def add_assistant_message(self, msg: dict) -> dict:
        msg = dict(msg)
        self._messages.append(msg)
        return msg

    def add_tool_message(self, tool_call_id: str, content: str) -> dict:
        msg = {"role": "tool", "tool_call_id": tool_call_id, "content": content}
        self._messages.append(msg)
        return msg

    # ── 查询方法 ─────────────────────────────────────

    def get_non_system_messages(self) -> list[dict]:
        """返回所有非 system 消息"""
        return [m for m in self._messages if m["role"] != "system"]

    def get_non_system_count(self) -> int:
        return sum(1 for m in self._messages if m["role"] != "system")

    def get_system_count(self) -> int:
        return sum(1 for m in self._messages if m["role"] == "system")

    def get_messages_for_llm(self) -> list[dict]:
        """返回传给 LLM 的消息（含 tool ordering 修复）"""
        if len(self._messages) <= 1:
            return list(self._messages)
        system = self._messages[0]
        repaired = self._repair_ordering(self._messages[1:])
        return [system] + repaired

    def get_last_assistant_message(self) -> dict | None:
        """获取最后一条 assistant 消息"""
        for m in reversed(self._messages):
            if m["role"] == "assistant":
                return m
        return None

    def get_last_content(self) -> str:
        """获取最后一条 assistant 消息的 content"""
        msg = self.get_last_assistant_message()
        return msg.get("content", "") if msg else ""

    # ── 工具消息顺序修复 ────────────────────────────

    @staticmethod
    def _repair_ordering(messages: list[dict]) -> list[dict]:
        """
        修复 tool 消息顺序 — 不丢弃信息，转为合成消息保留。

        当 tool_call 和 tool_result 不成对时（例如中断导致），
        将孤立的 tool 消息转为 system 消息保留信息。
        """
        result: list[dict] = []
        buffer: list[dict] = []
        active_tool_ids: set = set()

        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                tc_ids = {tc["id"] for tc in m["tool_calls"] if tc.get("id")}
                if buffer:
                    result.extend(buffer)
                    buffer = []
                active_tool_ids = tc_ids
                result.append(m)

            elif m["role"] == "tool":
                tid = m.get("tool_call_id", "")
                if tid in active_tool_ids:
                    if buffer:
                        result.extend(buffer)
                        buffer = []
                    result.append(m)
                    active_tool_ids.discard(tid)
                else:
                    buffer.append(m)

            else:
                if buffer:
                    result.extend(buffer)
                    buffer = []
                result.append(m)

        # 剩余的孤儿 tool 消息 → 合成 system 消息
        if buffer:
            combined = "以下工具返回结果（未被 LLM 处理）:\n"
            for bm in buffer:
                combined += f"- {bm.get('tool_call_id', '?')}: {bm.get('content', '')[:200]}\n"
            result.append({"role": "system", "content": combined.strip()})

        return result

    def repair_tool_ordering(self) -> int:
        """修复当前消息列表的 tool 顺序，返回修复/转换的消息数"""
        if len(self._messages) <= 1:
            return 0
        original = list(self._messages[1:])
        repaired = self._repair_ordering(original)
        changed = sum(1 for i, m in enumerate(original) if i < len(repaired) and m != repaired[i])
        if changed > 0:
            self._messages = [self._messages[0]] + repaired
        return changed

    def get_history_for_display(self) -> list[dict]:
        """获取用于显示的历史消息"""
        return self.get_non_system_messages()

    # ── 序列化 ───────────────────────────────────────

    def to_serializable(self) -> list[dict]:
        """返回可序列化的消息列表（用于持久化，跳过首条 system）"""
        result = []
        for i, msg in enumerate(self._messages):
            if msg.get("role") == "system" and i == 0:
                continue  # system prompt 由 SessionStore 重新加载
            save_msg = {"role": msg["role"], "content": msg.get("content", "")}
            for k in ("tool_calls", "tool_call_id", "reasoning_content"):
                if msg.get(k):
                    save_msg[k] = msg[k]
            result.append(save_msg)
        return result

    @classmethod
    def from_serialized(cls, system_prompt: str, serialized: list[dict]) -> "ConversationState":
        """从持久化数据恢复上下文"""
        state = cls(system_prompt)
        for msg in serialized:
            state._messages.append(msg)
        return state

    # ── Shortcircuit / scan 已迁移至 commands/shortcircuit.py ──
