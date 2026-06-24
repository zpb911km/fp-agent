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

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any


@dataclass
class CompactConfig:
    """压缩配置"""

    keep_meaningful: int = 4  # 保留的有意义（user/assistant）消息数
    max_summary_tokens: int = 300  # 摘要最大 token 数
    recent_margin: int = 20  # 保留的尾部消息数（fallback）


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

    def add_system_message(self, content: str) -> dict:
        msg = {"role": "system", "content": content}
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

    # ── 回退 ─────────────────────────────────────────

    def back(self, target_idx: int | None = None, mode: int | None = None) -> int:
        """
        回退到历史位置。

        Args:
            target_idx: 回退到的消息序号（1-based，从第一条非 system 消息开始）
                        None=交互模式（由上层处理）
            mode: 1=保留后续消息，2=删除后续消息

        Returns:
            删除的消息数量（0 表示未操作）
        """
        history = self.get_non_system_messages()
        if not history or target_idx is None:
            return 0

        if target_idx < 1 or target_idx > len(history):
            return 0

        idx_in_history = target_idx - 1  # 转为 0-based
        sys_count = self.get_system_count()

        if mode is None or mode == 2:
            # 删除后续消息
            deleted_count = len(self._messages) - (sys_count + idx_in_history + 1)
            del self._messages[sys_count + idx_in_history + 1 :]
            return deleted_count
        else:
            # mode == 1，保留后续消息（仅查看，不删除）
            return 0

    def get_history_for_display(self) -> list[dict]:
        """获取用于显示的历史消息"""
        return self.get_non_system_messages()

    # ── Fork ─────────────────────────────────────────

    def fork_snapshot(self) -> list[dict]:
        """返回当前上下文的完整快照（包括 system prompt）"""
        return list(self._messages)

    # ── 压缩 ─────────────────────────────────────────

    async def compact(
        self,
        summarizer: Callable[[str], Any],
        config: CompactConfig | None = None,
    ) -> tuple[bool, str]:
        """
        压缩上下文 — 压缩早期消息为摘要。

        Args:
            summarizer: 异步函数，接收压缩文本，返回摘要字符串（必选）
            config: 压缩配置

        Returns:
            (是否执行了压缩, 描述信息)

        Raises:
            ValueError: 如果 summarizer 为 None
        """
        if summarizer is None:
            raise ValueError("summarizer is required for compaction")

        if config is None:
            config = CompactConfig()

        history = self.get_non_system_messages()
        if len(history) <= config.keep_meaningful:
            return False, "对话历史较短，无需压缩"

        # 从尾部向前扫描，找第 N 条有意义消息的位置
        meaningful_found = 0
        split_idx = 0

        for i in range(len(history) - 1, -1, -1):
            role = history[i]["role"]
            if role in ("user", "assistant"):
                meaningful_found += 1
                if meaningful_found == config.keep_meaningful:
                    split_idx = i
                    break

        if meaningful_found < config.keep_meaningful:
            return False, "对话历史较短，无需压缩"

        to_compact = history[:split_idx]
        recent = history[split_idx:]

        if not to_compact:
            return False, "无需压缩"

        # 格式化待压缩消息
        compact_text = ""
        for m in to_compact:
            role_label = "用户" if m["role"] == "user" else "AI" if m["role"] == "assistant" else "工具"
            content = m.get("content", "")[:300]
            compact_text += f"[{role_label}]: {content}\n\n"

        # 调用 summarizer 生成摘要
        try:
            summary = await summarizer(compact_text)
            summary = (summary or "").strip()
        except Exception as e:
            return False, f"压缩失败: {e}"

        if not summary:
            return False, "压缩失败：摘要为空"

        # 重建上下文
        system = self._messages[0]
        self._messages = [system]
        self._messages.append({
            "role": "system",
            "content": f"以下是压缩后的对话历史摘要（省略了 {len(to_compact)} 条早期消息）：\n{summary}",
        })
        self._messages.extend(recent)

        return True, f"已压缩 {len(to_compact)} 条早期消息为摘要，保留 {len(recent)} 条"

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

    # ── 工具方法 ─────────────────────────────────────

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """估算 token 数量（简单近似）"""
        return len(text) // 3

    # ═══════════════════════════════════════════════
    # Shortcircuit（短路）
    # ═══════════════════════════════════════════════

    def scan_components(self) -> list[dict]:
        """
        扫描 _messages，返回从旧到新排序的连通块列表。

        连通块定义：以 user 消息为分隔符，从前向后切割。
        每个连通块从一条 user 消息开始，到下一个 user 的前一条或队列末尾结束。
        0 号位（system prompt）不计入。

        每条记录：
        {
            "idx": 1,                   # 连通块编号（1-based）
            "user_idx": 1,              # _messages 中的索引
            "terminal_idx": 2,          # _messages 中的索引（块终点）
            "message_count": 2,         # 该连通块包含的消息总数
            "user_preview": "帮我查天气",
            "assistant_preview": "北京25°C...",
            "compressible": True/False,  # 有实际内容可压缩（msg_count > 1）
            "complete": True/False,      # 最后一条是 assistant(无 tool_calls)
        }
        """
        # 收集所有 user 消息位置（跳过 system prompt 索引 0）
        user_indices = [i for i in range(1, len(self._messages)) if self._messages[i]["role"] == "user"]

        components = []
        for pos, user_idx in enumerate(user_indices):
            # 终点 = 下一个 user 的前一条，或队列末尾
            terminal_idx = user_indices[pos + 1] - 1 if pos + 1 < len(user_indices) else len(self._messages) - 1

            msg_count = terminal_idx - user_idx + 1
            terminal_msg = self._messages[terminal_idx]

            complete = terminal_msg["role"] == "assistant" and not terminal_msg.get("tool_calls")

            components.append({
                "idx": pos + 1,
                "user_idx": user_idx,
                "terminal_idx": terminal_idx,
                "message_count": msg_count,
                "user_preview": self._messages[user_idx].get("content", "")[:120],
                "assistant_preview": terminal_msg.get("content", "")[:120],
                "compressible": msg_count > 1,
                "complete": complete,
            })

        return components

    async def shortcircuit(
        self,
        refiner: Callable[[str, str, str], Any] | None,
        indices: list[tuple[int, int]],
        mode: str = "regenerate",
    ) -> tuple[bool, str, int]:
        """
        短路：将指定连通块压缩为 user + assistant 消息对。

        Args:
            refiner:  提炼回调，(user_text, assistant_text, context_text) → (new_user, new_assistant)
                      crop 模式传 None
            indices:  要短路的连通块消息索引 [(user_idx, terminal_idx), ...]
            mode:     "regenerate" | "crop"

        Returns:
            (是否成功, 描述信息, 压缩减少的消息数)
        """
        snapshot = list(self._messages)

        try:
            # ── Step 2: 逐块处理（全部成功才继续） ──
            new_sections: list[list[dict]] = []
            total_saved = 0

            for user_idx, terminal_idx in indices:
                user_msg = self._messages[user_idx]
                terminal_msg = self._messages[terminal_idx]
                msg_count = terminal_idx - user_idx + 1

                # ── 判断块是否完整（最后一条是 assistant 无 tool_calls） ──
                complete = terminal_msg["role"] == "assistant" and not terminal_msg.get("tool_calls")

                # ── 构建完整上下文（始终包含范围内所有消息） ──
                context_parts = []
                for j in range(user_idx, terminal_idx + 1):
                    m = self._messages[j]
                    role_label = "用户" if m["role"] == "user" else "AI" if m["role"] == "assistant" else "工具"
                    tc = " [调用工具]" if m.get("tool_calls") else ""
                    content = (m.get("content") or "")[:500]
                    context_parts.append(f"[{role_label}]{tc}: {content}")
                context_text = "\n\n".join(context_parts)

                if complete:
                    # ── 完整块：原有逻辑 ──
                    if msg_count == 2:
                        if mode == "crop":
                            new_sections.append([dict(user_msg), dict(terminal_msg)])
                        else:
                            assert refiner is not None
                            new_user, new_assistant = await refiner(
                                user_msg["content"], terminal_msg["content"], context_text
                            )
                            new_sections.append([
                                {"role": "user", "content": new_user},
                                {"role": "assistant", "content": new_assistant},
                            ])
                    else:
                        if mode == "crop" or refiner is None:
                            new_sections.append([
                                {"role": "user", "content": user_msg["content"]},
                                {"role": "assistant", "content": terminal_msg["content"]},
                            ])
                        else:
                            assert refiner is not None
                            new_user, new_assistant = await refiner(
                                user_msg["content"], terminal_msg["content"], context_text
                            )
                            new_sections.append([
                                {"role": "user", "content": new_user},
                                {"role": "assistant", "content": new_assistant},
                            ])
                else:
                    # ── 不完整块（中断）：安全压缩 ──
                    if mode == "crop" or refiner is None:
                        new_sections.append([
                            {"role": "user", "content": user_msg.get("content", "")},
                            {"role": "assistant", "content": "被用户中断"},
                        ])
                    else:
                        assert refiner is not None
                        new_user, new_assistant = await refiner(
                            user_msg.get("content", ""),
                            "_INTERRUPTED_",  # 标记中断，refiner 据此分流
                            context_text,
                        )
                        new_sections.append([
                            {"role": "user", "content": new_user},
                            {"role": "assistant", "content": new_assistant},
                        ])

                # ── 计算节省的消息数（msg_count <= 2 不计数） ──
                if msg_count > 2:
                    total_saved += msg_count - 2

            # ── Step 3: 一次性重建消息列表 ──
            new_messages = [self._messages[0]]  # system
            i = 1
            section_idx = 0

            while i < len(self._messages):
                if section_idx < len(indices) and i == indices[section_idx][0]:
                    for msg in new_sections[section_idx]:
                        new_messages.append(dict(msg))
                    i = indices[section_idx][1] + 1
                    section_idx += 1
                else:
                    new_messages.append(dict(self._messages[i]))
                    i += 1

            self._messages = new_messages
            return (True, "shortcircuit completed", total_saved)

        except Exception as e:
            self._messages = snapshot
            return (False, f"短路失败，已回滚: {e}", 0)
