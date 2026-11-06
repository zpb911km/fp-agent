"""测试 shortcircuit 插件（core.py）— v2 API：target(str) + action，工具层与命令层共用 apply_action

契约速查（本文件逐条对应）:
  1. degenerate 每个 target 恒产出至多两条：user=段内全部人类话语合并，assistant=段内全部叙事合并
  2. ⁂[kind] 注入（role=user）是工具异步输出 → 丢弃，不并进第一条 user
  3. tool 消息、assistant 的 tool_calls 壳 → 丢弃
  4. 段内没有人类话语 → 产物只剩一条 assistant
  5. protect_callsite=True 且段终点是进行中的调用点 → 该调用点保留 tool_calls，叙事并入其 content
  6/7. run_plan：段内整体 vs 段间分别；目标含当前块 → 该块强制 degenerate
  8. crop：首 user 原文 + 末 assistant 原文；不完整块 → "被用户中断"；msg_count==2 原样保留
  9~11. apply_action：list 只列不改；解析失败 ❌ + list；对账行 ✅/⚠️
  12~15. parse_target / parse_command / execute / format_components_display
"""

import asyncio
from collections.abc import Coroutine
from typing import Any, TypeVar

import pytest

from fp_core.core.jobs import INJECT_MARK
from fp_core.plugins.shortcircuit.core import (
    Message,
    apply_action,
    degenerate,
    execute,
    fmt_segments,
    format_components_display,
    parse_command,
    parse_target,
    run_plan,
    scan_components,
    shortcircuit,
)

_T = TypeVar("_T")


def _run(coro: Coroutine[Any, Any, _T]) -> _T:
    """同步跑一个协程（仓内约定：不引 pytest-asyncio，直接 asyncio.run）"""
    return asyncio.run(coro)


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


def _drained(content: str) -> Message:
    """忙时人类插话（环顶 drain 排空置位）：属于当前 run，不是块边界"""
    m = _msg("user", content)
    m["_drained"] = True
    return m


def _inj(kind: str, text: str) -> Message:
    """⁂[kind] 标记的系统注入（角色仍是 user，语义是工具异步输出）"""
    return _msg("user", f"{INJECT_MARK}{kind}] {text}")


# ═══════════════════════════════════════════════════════
# 假 state（apply_action / run_plan / execute 的副作用记录器）
# ═══════════════════════════════════════════════════════


class _FakeConversation:
    def __init__(self, messages: list[Message]) -> None:
        self._messages = list(messages)
        self.system_prompt: object = {"role": "system", "content": "sys"}
        self.set_messages_calls = 0

    def get_non_system_messages(self) -> list[Message]:
        return list(self._messages)

    def set_messages(self, system_prompt: object, messages: list[Message]) -> None:
        self.system_prompt = system_prompt
        self._messages = list(messages)
        self.set_messages_calls += 1

    def to_serializable(self) -> dict[str, object]:
        return {"messages": self._messages}


class _FakeSession:
    def __init__(self) -> None:
        self.saved: list[dict[str, object]] = []

    def save_context(self, data: dict[str, object]) -> None:
        self.saved.append(data)


class _FakeLLM:
    """可注入假返回 —— build_regenerate_refiner 走 state.llm.summarize"""

    def __init__(self, reply: str = "REFINED") -> None:
        self.reply = reply
        self.calls: list[dict[str, object]] = []

    async def summarize(self, text: str, **kwargs: object) -> str:
        self.calls.append({"text": text, **kwargs})
        return self.reply


class _FakeState:
    def __init__(self, messages: list[Message], llm_reply: str = "REFINED") -> None:
        self.conversation = _FakeConversation(messages)
        self.session = _FakeSession()
        self.llm = _FakeLLM(llm_reply)

    @property
    def messages(self) -> list[Message]:
        # 测试替身有意镜像真实 Conversation 的私有字段 _messages（两者同为本文件内的协作替身），
        # 类型层面无可标注的写法，此处按最小范围抑制
        return self.conversation._messages  # pyright: ignore[reportPrivateUsage]

    @property
    def write_backs(self) -> int:
        return self.conversation.set_messages_calls


# ═══════════════════════════════════════════════════════
# scan_components — 连通块边界与标记
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
        assert [(c["idx"], c["user_idx"], c["terminal_idx"], c["message_count"]) for c in comps] == [
            (1, 0, 1, 2),
            (2, 2, 3, 2),
        ]
        assert comps[0]["user_preview"] == "任务A"
        assert comps[0]["assistant_preview"] == "回复A"

    def test_degenerable_and_compressible_flags(self):
        messages: list[Message] = [
            _msg("user", "任务"),
            _msg("assistant", "", tool_calls=_tc("s1")),
            _msg("tool", "结果"),
            _msg("assistant", "总结"),
        ]
        comp = scan_components(messages)[0]
        assert comp["degenerable"] is True, "块内有工具往返"
        assert comp["compressible"] is True, "4 条 > 2，有压缩空间"
        assert comp["complete"] is True, "终点是纯文本 assistant"

    def test_plain_block_not_degenerable(self):
        messages: list[Message] = [_msg("user", "任务"), _msg("assistant", "回复")]
        comp = scan_components(messages)[0]
        assert comp["degenerable"] is False
        assert comp["compressible"] is False

    def test_incomplete_block_marked(self):
        messages: list[Message] = [_msg("user", "任务"), _msg("assistant", "", tool_calls=_tc("s1"))]
        comp = scan_components(messages)[0]
        assert comp["complete"] is False, "终点是进行中的调用点"
        assert comp["degenerable"] is True

    def test_injected_does_not_open_new_block(self):
        """⁂[kind] 注入不是人类轮次 → 不开新块，并入前一个连通块"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "回复A"),
            _inj("job_done", "后台任务完成"),
            _msg("assistant", "已报告"),
            _msg("user", "任务B"),
            _msg("assistant", "回复B"),
        ]
        comps = scan_components(messages)
        assert len(comps) == 2, "注入消息不产生新块"
        assert comps[0]["user_preview"] == "任务A"
        assert comps[0]["message_count"] == 4, "注入消息并回上一人类轮次"
        assert comps[0]["user_idx"] == 0 and comps[0]["terminal_idx"] == 3
        assert comps[1]["user_preview"] == "任务B"

    def test_leading_injected_floats_before_all_blocks(self):
        """首条人类消息之前的注入游离于所有块之外（保守保留，不参与压缩）"""
        messages: list[Message] = [
            _inj("peer_ring", "铃声"),
            _msg("assistant", "已接听"),
            _msg("user", "任务"),
            _msg("assistant", "回复"),
        ]
        comps = scan_components(messages)
        assert len(comps) == 1
        assert comps[0]["user_idx"] == 2
        assert comps[0]["terminal_idx"] == 3

    def test_drained_human_does_not_open_new_block(self):
        """_drained 忙时插话属于当前 run → 不切块（否则前段 run 成假『被用户中断』）"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "", tool_calls=_tc("s1")),
            _msg("tool", "结果1"),
            _drained("顺便看下X"),
            _msg("assistant", "完成"),
            _msg("user", "任务B"),
            _msg("assistant", "回复B"),
        ]
        comps = scan_components(messages)
        assert len(comps) == 2, "_drained 人类消息不产生新块"
        assert comps[0]["message_count"] == 5
        assert comps[0]["complete"] is True, "块终点是真收尾 assistant"


# ═══════════════════════════════════════════════════════
# degenerate — 无损塌结构：人类话语一条、叙事一条
# ═══════════════════════════════════════════════════════


class TestDegenerate:
    def test_merges_human_and_narrative_drops_noise(self):
        """契约 1+3：一段 → 两条；叙事合并（含被剥壳的调用点 content）；tool 与壳丢弃"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("s1")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成"),
        ]
        ok, _desc, changed, new = degenerate(messages, [(0, 3)])
        assert ok
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant"]
        assert new[0] == {"role": "user", "content": "任务A"}
        assert new[1] == {"role": "assistant", "content": "A开始\nA完成"}, "调用点壳剥掉但 content 并入叙事"
        assert changed == 2, "1 条 tool 丢弃 + 1 条调用点剥壳"

    def test_injected_is_discarded(self):
        """契约 2：⁂[kind] 虽是 user 角色，属工具异步输出 → 丢弃，不并进第一条 user"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "", tool_calls=_tc("s1")),
            _msg("tool", "A结果"),
            _inj("job_done", "后台完成"),
            _msg("assistant", "回复A"),
        ]
        ok, _desc, changed, new = degenerate(messages, [(0, 4)])
        assert ok
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant"]
        assert new[0] == {"role": "user", "content": "任务A"}, "注入不能并进人类话语"
        assert new[1] == {"role": "assistant", "content": "回复A"}, "注入是工具输出，不是我的叙事"
        assert all(INJECT_MARK not in (m.get("content") or "") for m in new)
        assert changed == 3, "tool + 空调用点 + 注入各一"

    def test_drained_human_merged_into_first_user(self):
        """契约 1：忙时插话也是人类话语，按时间正序 "\n" 并入第一条 user"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("s1")),
            _msg("tool", "A结果"),
            _drained("顺便看下X"),
            _msg("assistant", "A完成"),
        ]
        ok, _desc, _changed, new = degenerate(messages, [(0, 4)])
        assert ok
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant"]
        assert new[0]["content"] == "任务A\n顺便看下X"
        assert new[1]["content"] == "A开始\nA完成"

    def test_segment_without_human_turns_yields_single_assistant(self):
        """契约 4：段内只有注入 + 我的叙事 → 产物只剩一条 assistant"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _inj("peer_ring", "铃声"),
            _msg("assistant", "已接听"),
        ]
        ok, _desc, _changed, new = degenerate(messages, [(2, 3)])
        assert ok
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant", "assistant"]
        assert new[1]["content"] == "A完成"
        assert new[2] == {"role": "assistant", "content": "已接听"}, "无人类话语 → 不产 user"
        assert all(INJECT_MARK not in (m.get("content") or "") for m in new)

    def test_protect_callsite_keeps_in_flight_call(self):
        """契约 5：段终点是进行中的调用点 → 原样保留 tool_calls，叙事并入其 content（叙事在前）"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("s1")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成", tool_calls=_tc("sc")),
        ]
        ok, _desc, _changed, new = degenerate(messages, [(0, 3)], protect_callsite=True)
        assert ok
        assert new is not None
        assert len(new) == 2, "调用点并入后仍只有 user + 调用点"
        assert new[0] == {"role": "user", "content": "任务A"}
        assert new[1]["role"] == "assistant"
        assert new[1]["tool_calls"][0]["function"]["name"] == "sc", "进行中的调用点本轮不动"
        assert new[1]["content"] == "A开始\nA完成", "叙事在前、原标题在后"
        assert not any(m["role"] == "tool" for m in new)

    def test_protect_callsite_false_strips_in_flight_call(self):
        """契约 3/5 对照：命令情境不保护 → 调用点也剥壳成纯文本叙事"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("s1")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成", tool_calls=_tc("sc")),
        ]
        ok, _desc, _changed, new = degenerate(messages, [(0, 3)], protect_callsite=False)
        assert ok
        assert new is not None
        assert new == [
            {"role": "user", "content": "任务A"},
            {"role": "assistant", "content": "A开始\nA完成"},
        ]

    def test_multiple_targets_yield_one_pair_each(self):
        """契约 1：每个 target 各产一对 → 多 target = 多对（段粒度决定产物对数）"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("s1")),
            _msg("tool", "A结果"),
            _drained("顺便看下X"),
            _inj("job_done", "后台完成"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B完成"),
        ]
        ok, _desc, _changed, new = degenerate(messages, [(0, 5), (6, 7)])
        assert ok
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant", "user", "assistant"]
        assert new[0]["content"] == "任务A\n顺便看下X"
        assert new[1]["content"] == "A开始\nA完成"
        assert new[2]["content"] == "任务B"
        assert new[3]["content"] == "B完成"

    def test_single_target_spanning_blocks_yields_one_pair(self):
        """契约 6（原语层）：一个 target 跨多块 → 一对；文字全留，只塌结构"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("s1")),
            _msg("tool", "A结果"),
            _drained("顺便看下X"),
            _inj("job_done", "后台完成"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B完成"),
        ]
        ok, _desc, _changed, new = degenerate(messages, [(0, 7)])
        assert ok
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant"], "整段合成一问一答"
        assert new[0]["content"] == "任务A\n顺便看下X\n任务B"
        assert new[1]["content"] == "A开始\nA完成\nB完成"


# ═══════════════════════════════════════════════════════
# shortcircuit — crop（只要首尾）/ regenerate（LLM 提炼）
# ═══════════════════════════════════════════════════════


class TestCrop:
    def test_complete_block_takes_first_user_and_last_assistant(self):
        """契约 8：完整块 → 首 user 原文 + 末 assistant 原文，中间一切全丢"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "中间想了一步"),
            _inj("job_done", "后台完成"),
            _msg("assistant", "最终结论"),
        ]
        ok, _desc, saved, new = _run(shortcircuit(messages, None, [(0, 3)], "crop"))
        assert ok
        assert new == [
            {"role": "user", "content": "任务A"},
            {"role": "assistant", "content": "最终结论"},
        ]
        assert saved == 2

    def test_range_across_blocks_is_one_pair(self):
        """契约 6/8：target 跨块 → 首块 user + 末块 assistant，中间的人类插话也丢"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _drained("顺便看下X"),
            _msg("assistant", "B完成"),
        ]
        ok, _desc, saved, new = _run(shortcircuit(messages, None, [(0, 4)], "crop"))
        assert ok
        assert new == [
            {"role": "user", "content": "任务A"},
            {"role": "assistant", "content": "B完成"},
        ], "crop 是机械的首尾取值，不拼中间 user"
        assert saved == 3

    def test_incomplete_block_marked_interrupted(self):
        """契约 8：不完整块 → user + 『被用户中断』"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A进行中", tool_calls=_tc("s1")),
            _msg("tool", "A结果"),
        ]
        ok, _desc, saved, new = _run(shortcircuit(messages, None, [(0, 2)], "crop"))
        assert ok
        assert new == [
            {"role": "user", "content": "任务A"},
            {"role": "assistant", "content": "被用户中断"},
        ]
        assert saved == 1

    def test_two_message_block_preserved_verbatim(self):
        """契约 8：msg_count==2 原样保留（含额外字段），不重建 dict"""
        messages: list[Message] = [
            {"role": "user", "content": "任务A", "id": "u1"},
            {"role": "assistant", "content": "A完成", "id": "a1"},
        ]
        ok, _desc, saved, new = _run(shortcircuit(messages, None, [(0, 1)], "crop"))
        assert ok
        assert new == messages
        assert new is not None and new[0].get("id") == "u1", "原消息原样保留"
        assert saved == 0

    def test_regenerate_uses_refiner_and_labels_injected_as_system(self):
        """regenerate：整段交 LLM 提炼成一对；上下文里注入标注 [系统]、人类标注 [用户]"""
        captured: dict[str, str] = {}

        async def refiner(user_text: str, assistant_text: str, context_text: str) -> tuple[str, str]:
            captured["user"] = user_text
            captured["assistant"] = assistant_text
            captured["ctx"] = context_text
            return ("提炼后的问题", "提炼后的回复")

        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "", tool_calls=_tc("s1")),
            _msg("tool", "A结果"),
            _inj("job_done", "后台完成"),
            _msg("assistant", "A完成"),
        ]
        ok, _desc, _saved, new = _run(shortcircuit(messages, refiner, [(0, 4)], "regenerate"))
        assert ok
        assert new == [
            {"role": "user", "content": "提炼后的问题"},
            {"role": "assistant", "content": "提炼后的回复"},
        ]
        assert captured["user"] == "任务A" and captured["assistant"] == "A完成", "首尾原文交给 refiner"
        assert "[用户]: 任务A" in captured["ctx"]
        assert "[系统]: " in captured["ctx"] and "后台完成" in captured["ctx"]


# ═══════════════════════════════════════════════════════
# parse_target / fmt_segments — target 语法（宽松解析）
# ═══════════════════════════════════════════════════════


class TestParseTarget:
    @pytest.mark.parametrize(
        ("spec", "expected"),
        [
            ("7", [(7, 7)]),
            ("#7", [(7, 7)]),
            ("＃7", [(7, 7)]),
            ("#3-9", [(3, 9)]),
            ("#3-#9", [(3, 9)]),
            ("＃3~9", [(3, 9)]),
            ("#3 — 9", [(3, 9)]),
            ("3, 7-9", [(3, 3), (7, 9)]),
            ("#3，7", [(3, 3), (7, 7)]),
        ],
    )
    def test_valid_forms(self, spec: str, expected: list[tuple[int, int]]):
        segments, err = parse_target(spec)
        assert err is None
        assert segments == expected

    @pytest.mark.parametrize(
        ("spec", "keyword"),
        [
            ("", "空"),
            ("abc", "解析"),
            ("#9-3", "大于"),
            ("#0", "≥ 1"),
        ],
    )
    def test_invalid_forms(self, spec: str, keyword: str):
        segments, err = parse_target(spec)
        assert segments is None
        assert err is not None and keyword in err


class TestFmtSegments:
    @pytest.mark.parametrize(
        ("segments", "expected"),
        [
            ([(7, 7)], "#7"),
            ([(3, 9)], "#3-9"),
            ([(3, 3), (7, 9)], "#3,#7-9"),
        ],
    )
    def test_render(self, segments: list[tuple[int, int]], expected: str):
        assert fmt_segments(segments) == expected


# ═══════════════════════════════════════════════════════
# run_plan — 段粒度 / 当前块强制 degenerate / regenerate
# ═══════════════════════════════════════════════════════


def _three_tool_blocks() -> list[Message]:
    """3 个各含一次工具往返的连通块（每块 4 条）"""
    messages: list[Message] = []
    for name in ("A", "B", "C"):
        messages.extend([
            _msg("user", f"任务{name}"),
            _msg("assistant", f"{name}开始", tool_calls=_tc(name)),
            _msg("tool", f"{name}结果"),
            _msg("assistant", f"{name}完成"),
        ])
    return messages


class TestRunPlan:
    def test_segment_spanning_blocks_collapses_to_one_pair(self):
        """契约 6：[(1,3)] → 1 对；[(1,1),(2,3)] → 2 对（差别只在 target 粒度）"""
        messages = _three_tool_blocks()
        ok, msg, saved, new = _run(run_plan(messages, _FakeState(messages), "degenerate", [(1, 3)], True))
        assert ok
        assert msg.startswith("✅ degenerate #1-3 · 消息 12→2 条（省 10）")
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant"], "整段应合成一对"
        assert new[0]["content"] == "任务A\n任务B\n任务C"
        assert saved == 10

        ok, _desc, _saved, new2 = _run(run_plan(messages, _FakeState(messages), "degenerate", [(1, 1), (2, 3)], True))
        assert ok
        assert new2 is not None
        assert [m["role"] for m in new2] == ["user", "assistant", "user", "assistant"], "两段各一对"

    def test_multi_segment_processed_separately(self):
        """契约 6 段间语义：多段各处理各的，未命中的块原样保留"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("a")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B1"),
            _msg("assistant", "B2"),
            _msg("user", "任务C"),
            _msg("assistant", "C开始", tool_calls=_tc("c")),
            _msg("tool", "C结果"),
            _msg("assistant", "C完成"),
        ]
        ok, msg, saved, new = _run(run_plan(messages, _FakeState(messages), "degenerate", [(1, 1), (3, 3)], True))
        assert ok
        assert msg == "✅ degenerate #1,#3 · 消息 11→7 条（省 4）｜剩余可退化 无", "块编号用 fmt_segments 形式"
        assert saved == 4
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant", "user", "assistant", "assistant", "user", "assistant"]
        assert new[0]["content"] == "任务A" and new[1]["content"] == "A开始\nA完成"
        assert [m["content"] for m in new[2:5]] == ["任务B", "B1", "B2"], "未命中的 #2 原样保留"
        assert new[5]["content"] == "任务C" and new[6]["content"] == "C开始\nC完成"

    def test_current_block_forced_degenerate_even_for_crop(self):
        """契约 7：目标含当前块 → 该块强制 degenerate；其他块按请求的 crop 处理"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A中间"),
            _drained("A插话"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _drained("B插话"),
            _msg("assistant", "B完成"),
        ]
        ok, msg, saved, new = _run(run_plan(messages, _FakeState(messages), "crop", [(1, 2)], True))
        assert ok
        assert msg == "✅ crop #1-2 · 消息 9→4 条（省 5）｜剩余可退化 无"
        assert saved == 5
        assert new is not None
        assert [m["role"] for m in new] == ["user", "assistant", "user", "assistant"]
        # #1 按 crop：中间叙事与中间人类插话全丢
        assert new[0] == {"role": "user", "content": "任务A"}
        assert new[1] == {"role": "assistant", "content": "A完成"}
        # #2 当前块强制 degenerate：中间人类插话保留、工具往返清掉
        assert new[2]["content"] == "任务B\nB插话", "degenerate 不丢中间 user —— 这就是 crop 覆盖不到的形态"
        assert new[3]["content"] == "B开始\nB完成"
        assert not any(m["role"] == "tool" for m in new)

    def test_regenerate_builds_refiner_from_state_llm(self):
        """regenerate 段：refiner 由 state.llm.summarize 驱动，user 原文保留、assistant 用提炼结果"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A中间"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B完成"),
        ]
        st = _FakeState(messages, llm_reply="我做了A并得出结论")
        ok, msg, saved, new = _run(run_plan(messages, st, "regenerate", [(1, 1)], True))
        assert ok
        assert msg == "✅ regenerate #1 · 消息 5→4 条（省 1）｜剩余可退化 无"
        assert saved == 1
        assert len(st.llm.calls) == 1
        assert "[用户]: 任务A" in str(st.llm.calls[0]["text"])
        assert "instruction" in st.llm.calls[0]
        assert new == [
            {"role": "user", "content": "任务A"},
            {"role": "assistant", "content": "我做了A并得出结论"},
            {"role": "user", "content": "任务B"},
            {"role": "assistant", "content": "B完成"},
        ]

    def test_protect_callsite_forwarded_to_current_block(self):
        """契约 5/14：protect_callsite 只作用于当前块 —— 工具情境保留进行中的调用点"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
        ]
        ok, _desc, _saved, protected = _run(run_plan(messages, _FakeState(messages), "degenerate", [(2, 2)], True))
        assert ok
        assert protected is not None
        assert protected[-1]["tool_calls"][0]["function"]["name"] == "b", "调用点要留给 loop"

        ok, _desc, _saved, stripped = _run(run_plan(messages, _FakeState(messages), "degenerate", [(2, 2)], False))
        assert ok
        assert stripped is not None
        assert "tool_calls" not in stripped[-1]
        assert stripped[-1]["content"] == "B开始"


# ═══════════════════════════════════════════════════════
# apply_action — 工具层/命令层唯一执行入口（list / 解析失败 / 写回）
# ═══════════════════════════════════════════════════════


def _one_tool_block() -> list[Message]:
    return [
        _msg("user", "任务A"),
        _msg("assistant", "A开始", tool_calls=_tc("a")),
        _msg("tool", "A结果"),
        _msg("assistant", "A完成"),
    ]


class TestApplyAction:
    def test_list_only_reads(self):
        """契约 9：action=list 只返回列表，不写回"""
        messages = _one_tool_block()
        st = _FakeState(messages)
        ok, text = _run(apply_action(st.messages, st, "list", "", True))
        assert ok
        assert text.startswith("## 📦 连通块 共1 · 可退化1 · 当前块#1（只能退化）")
        assert st.write_backs == 0
        assert st.session.saved == []

    def test_list_with_target_appends_hint(self):
        """契约 9：list 同时给了 target → 末尾提示『只列不改』"""
        messages = _one_tool_block()
        st = _FakeState(messages)
        ok, text = _run(apply_action(st.messages, st, "list", "#1", True))
        assert ok
        assert "## 📦" in text
        assert text.splitlines()[-1].startswith("（action=list 只列不改")
        assert st.write_backs == 0

    def test_invalid_target_returns_error_with_listing(self):
        """契约 10：解析失败 → (True, ❌ 开头 + 当前 list)，不写回"""
        messages = _one_tool_block()
        st = _FakeState(messages)
        ok, text = _run(apply_action(st.messages, st, "degenerate", "abc", True))
        assert ok is True, "解析失败不是异常路径，回文本让模型一次改对"
        assert text.startswith("❌")
        assert "## 📦" in text
        assert st.write_backs == 0
        assert st.session.saved == []

    def test_success_writes_back_conversation_and_session(self):
        """契约 11：成功 → 写回 conversation.set_messages + session.save_context，回对账行"""
        messages = _one_tool_block()
        st = _FakeState(messages)
        ok, text = _run(apply_action(st.messages, st, "degenerate", "#1", True))
        assert ok
        assert text == "✅ degenerate #1 · 消息 4→2 条（省 2）｜剩余可退化 无"
        assert st.write_backs == 1
        assert len(st.session.saved) == 1
        assert st.messages == [
            {"role": "user", "content": "任务A"},
            {"role": "assistant", "content": "A开始\nA完成"},
        ]


# ═══════════════════════════════════════════════════════
# 对账行 —— 模型靠它判断有没有真的剪到东西（契约 11）
# ═══════════════════════════════════════════════════════


class TestReconciliationLine:
    def test_reports_exact_counts_and_remaining(self):
        """✅ 动作 + 块编号 · 消息 A→B 条（省 N）｜剩余可退化 #M"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("a")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
            _msg("tool", "B结果"),
            _msg("assistant", "B完成"),
            _msg("user", "任务C"),
            _msg("assistant", "C完成"),
        ]
        _ok, text = _run(apply_action(messages, _FakeState(messages), "degenerate", "#1", True))
        assert text == "✅ degenerate #1 · 消息 10→8 条（省 2）｜剩余可退化 #2"

    def test_zero_saved_warns_instead_of_ok(self):
        """契约 11：saved==0 → ⚠️ 开头 + 明说没剪到任何消息"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B完成"),
        ]
        _ok, text = _run(apply_action(messages, _FakeState(messages), "crop", "#1", True))
        assert text.startswith("⚠️ crop #1 · 消息 4→4 条（省 0）"), "0 变化不能给 ✅"
        assert "未剪到任何消息" in text


# ═══════════════════════════════════════════════════════
# parse_command / execute —— 命令层 /sc（与工具层同一 apply_action）
# ═══════════════════════════════════════════════════════


class TestParseCommand:
    @pytest.mark.parametrize(
        ("arg", "expected"),
        [
            ("", ("list", "")),
            ("list", ("list", "")),
            ("#3-9", ("list", "#3-9")),
            ("-d #3-9", ("degenerate", "#3-9")),
            ("-c #7", ("crop", "#7")),
            ("-r #1-3", ("regenerate", "#1-3")),
            ("--degenerate #7", ("degenerate", "#7")),
        ],
    )
    def test_parse(self, arg: str, expected: tuple[str, str]):
        assert parse_command(arg) == expected


class TestExecuteCommand:
    def test_plain_sc_lists_without_write_back(self):
        """契约 14：命令层走同一 apply_action；/sc 缺省 = list，不写回"""
        messages = _one_tool_block()
        st = _FakeState(messages)
        ok, text = _run(execute(st, ""))
        assert ok
        assert text.startswith("## 📦 连通块 共1")
        assert st.write_backs == 0

    def test_degenerate_returns_reconciliation_line_and_writes_back(self):
        """契约 14：/sc -d #N → 与工具层同一形态的对账行 + 写回"""
        messages = [
            _msg("user", "任务A"),
            _msg("assistant", "A开始", tool_calls=_tc("a")),
            _msg("tool", "A结果"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B完成"),
        ]
        st = _FakeState(messages)
        ok, text = _run(execute(st, "-d #1"))
        assert ok
        assert text == "✅ degenerate #1 · 消息 6→4 条（省 2）｜剩余可退化 无"
        assert st.write_backs == 1
        assert [m["role"] for m in st.messages] == ["user", "assistant", "user", "assistant"]

    def test_command_layer_does_not_protect_callsite(self):
        """契约 14：两层唯一差别 —— 命令层 protect_callsite=False，进行中的调用点也剥壳"""
        messages: list[Message] = [
            _msg("user", "任务A"),
            _msg("assistant", "A完成"),
            _msg("user", "任务B"),
            _msg("assistant", "B开始", tool_calls=_tc("b")),
        ]
        cmd_state = _FakeState(messages)
        _ok, cmd_text = _run(execute(cmd_state, "-d #2"))
        assert cmd_text.startswith("⚠️ degenerate #2"), "进行中调用点剥壳不减少条数"
        assert "tool_calls" not in cmd_state.messages[-1]

        tool_state = _FakeState(messages)
        _run(apply_action(messages, tool_state, "degenerate", "#2", True))
        assert tool_state.messages[-1]["tool_calls"][0]["function"]["name"] == "b", "工具情境保留调用点"


# ═══════════════════════════════════════════════════════
# format_components_display —— 一行一块，结构优先（契约 15）
# ═══════════════════════════════════════════════════════


class TestFormatComponentsDisplay:
    def _four_state_messages(self) -> list[Message]:
        return [
            # #1 D：有工具往返
            _msg("user", "D块"),
            _msg("assistant", "开始", tool_calls=_tc("a")),
            _msg("tool", "T"),
            _msg("assistant", "完成"),
            # #2 C：可压缩、无工具往返
            _msg("user", "C块"),
            _msg("assistant", "中"),
            _msg("assistant", "尾"),
            # #3 ~：已压缩
            _msg("user", "~块"),
            _msg("assistant", "完"),
            # #4 *：未完成（当前块）
            _msg("user", "未完成块"),
        ]

    def test_first_line_structure(self):
        lines = format_components_display(scan_components(self._four_state_messages())).splitlines()
        assert lines[0] == "## 📦 连通块 共4 · 可退化1 · 当前块#4（只能退化）"

    def test_single_line_per_block_with_four_states(self):
        lines = format_components_display(scan_components(self._four_state_messages())).splitlines()
        assert lines[2].startswith("- **#1** `D` 4条 · D块 ｜ AI: 完成")
        assert lines[3].startswith("- **#2** `C` 3条 · C块 ｜ AI: 尾")
        assert lines[4].startswith("- **#3** `~` 2条 · ~块 ｜ AI: 完")
        assert lines[5].startswith("- **#4** `*` 1条 · 未完成块")
        assert len(lines) == 6, "每块一行，摘要不能占第二行"
