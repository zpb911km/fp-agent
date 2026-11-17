"""回归：同一条最终回复不得在消息流里显示两次。

用户侧症状：任务完成后，偶发看到最终回复重复出现（不是每次都出现）。

根因（fp-core `tests/test_stream_event_order.py` 有后端侧锚点）：两条投递路径的
落地时机不同 —— `WebSocketIO._pub` 是 `ensure_future`（**排队**），`EventBridge`
是 `await publish`（**同步落地**）。`io.stream_end()` 之后紧随的 llm_end 于是越过
还在缓冲里的末批 token。真机抓到的帧序：`llm_end → done → thinking_content →
chunk → stream_end`。前端把迟到正文并进"当前轮"的卡片；**跨轮时**上一轮答案就落进
新卡片 → 同一条回复显示两次。

判据：**逐案声明期望的助手卡片文本序列**，要求与 DOM 里实际卡片逐字相等。
为什么不用「子串出现次数」这类启发式（上一版就是这么写的，结果是空壳）：
真机症状常是**拼接**——`答案一` 只剩 3 字，长度护栏一挡就跳过了，于是带着 bug
也全绿。精确序列比对没有这个盲区：`["答案一答案二"]` != `["答案一", "答案二"]`。
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.e2e


def _dispatch(page, event):
    page.evaluate("e => window.__fp_dispatch(e)", event)


def _isolate(page):
    """隔离「会话历史」这条数据源：REST 打空桩 + 清空消息容器。

    为什么必须：`server` fixture 可能**复用外部开发服务**（那是用户正在用的实例，
    会话里有真实对话）。本文件断言的是「这一串事件应该渲染出哪几张卡」，若容器里
    还躺着别的历史，期望值就得随「那个服务里恰好有什么」变化——实测吃到过：
    全量跑时 17 案全红（外部服务里有一段真实测试对话）、单独跑全绿，差别只在历史。
    空桩让用例与「用哪个服务器、它里面有什么」彻底无关。
    """
    page.route(
        "**/api/sessions/*/messages*",
        lambda route: route.fulfill(status=200, content_type="application/json", body='{"messages": []}'),
    )
    # 登录成功那一刻 bootstrap 已经发出过一次历史请求（早于上面那条 route），
    # 它的响应可能**晚于**这里落地并渲染 → 先等它跑完再清，否则清了又长回来。
    page.wait_for_timeout(800)
    page.evaluate(
        """() => {
             const el = document.getElementById('messagesContainer');
             if (el) el.innerHTML = '';
           }"""
    )


def _assistant_texts(page):
    """助手卡片正文，按 DOM 顺序（思考块不算：它有自己的容器类名）"""
    return [t.strip() for t in page.locator(".assistant-msg .msg-content").all_inner_texts() if t.strip()]


def _run(page, case):
    _isolate(page)
    for ev in case["events"]:
        _dispatch(page, ev)
        page.wait_for_timeout(60)  # 让节流 flush 有机会跑，贴近真实到达节奏
    page.wait_for_timeout(case.get("settle", 600))


def _check(page, case):
    actual = _assistant_texts(page)
    print(f"\n[{case['name']}] 期望 {case['expect']} / 实际 {actual}")
    assert actual == case["expect"], (
        f"序列 {case['name']} 渲染不符\n  期望: {case['expect']}\n  实际: {actual}\n"
        f"  事件: {[e['type'] for e in case['events']]}"
    )


# ══════════════════════════════════════════════════════════════
# 一、单轮：只有一张卡，文本必须逐字等于后端给的权威全文
# ══════════════════════════════════════════════════════════════
SINGLE_TURN = [
    {
        "name": "basic",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终"},
            {"type": "chunk", "content": "回复正文"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
        ],
        "expect": ["最终回复正文"],
    },
    {
        # llm_end 之后又到一个迟到 chunk（fire-and-forget 排序）：同一轮内并入原卡
        "name": "late_chunk_after_llm_end",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "chunk", "content": "回复正文"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
        ],
        "expect": ["最终回复正文"],
    },
    {
        "name": "late_chunk_after_done",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
            {"type": "chunk", "content": "回复正文"},
        ],
        "expect": ["最终回复正文"],
    },
    {
        "name": "no_chunks",
        "events": [
            {"type": "llm_start"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
        ],
        "expect": ["最终回复正文"],
    },
    {
        "name": "thinking_only_then_done",
        "events": [
            {"type": "llm_start"},
            {"type": "thinking_content", "token": "让我想想"},
            {"type": "llm_end", "content": ""},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
        ],
        "expect": ["最终回复正文"],
    },
    {
        "name": "error_then_done",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终回复正文"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "error", "error": "boom"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
        ],
        "expect": ["最终回复正文"],
    },
    {
        "name": "cancelled_then_done",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终回复正文"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "cancelled"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
        ],
        "expect": ["最终回复正文"],
    },
    {
        # FP 干完活写总结的典型形状：工具轮 → 最终文本轮
        "name": "tool_then_final",
        "events": [
            {"type": "llm_start"},
            {"type": "llm_end", "content": "", "has_tool_calls": True, "tool_names": ["bash"]},
            {"type": "tool_select", "tools": [{"name": "bash"}]},
            {"type": "tool_call", "name": "bash", "args": {"command": "ls"}, "tool_call_id": "c1"},
            {"type": "tool_result", "name": "bash", "result": "ok", "tool_call_id": "c1"},
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终回复"},
            {"type": "chunk", "content": "正文"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 4},
        ],
        "expect": ["最终回复正文"],
    },
    {
        # 最后一轮带 tool_calls 且带文本：done 的 final_content 不得再另起一张卡
        "name": "last_round_tool_calls_with_text",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终回复正文"},
            {"type": "llm_end", "content": "最终回复正文", "has_tool_calls": True, "tool_names": ["bash"]},
            {"type": "tool_select", "tools": [{"name": "bash"}]},
            {"type": "tool_call", "name": "bash", "args": {"command": "ls"}, "tool_call_id": "c1"},
            {"type": "tool_result", "name": "bash", "result": "ok", "tool_call_id": "c1"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 4},
        ],
        "expect": ["最终回复正文"],
    },
]

# ══════════════════════════════════════════════════════════════
# 二、跨轮迟到帧：**本 bug 的真实现场**
#   第 1 轮已终结并落定答案，第 2 轮已开新卡，此时第 1 轮的末批 token 才到。
# ══════════════════════════════════════════════════════════════
CROSS_TURN = [
    {
        # 第 2 轮自己没有正文 chunk（全被批量缓冲）→ 卡片由 llm_end 建，
        # 随后第 1 轮的迟到 chunk 落进这张卡 → 卡片变「答案一答案二」
        "name": "late_chunk_into_next_round_card",
        "events": [
            {"type": "llm_start"},
            {"type": "llm_end", "content": "答案一"},
            {"type": "done", "final_content": "答案一", "non_system_count": 2},
            {"type": "user_message", "content": "第二轮提问"},
            {"type": "llm_start"},
            {"type": "llm_end", "content": "答案二"},
            {"type": "done", "final_content": "答案二", "non_system_count": 4},
            {"type": "chunk", "content": "答案一"},
            {"type": "chunk", "content": "答案二"},
        ],
        "expect": ["答案一", "答案二"],
    },
    {
        # 迟到 chunk 紧跟下一轮 llm_start（此时新卡还没建）
        "name": "late_chunk_right_after_next_start",
        "events": [
            {"type": "llm_start"},
            {"type": "llm_end", "content": "答案一"},
            {"type": "done", "final_content": "答案一", "non_system_count": 2},
            {"type": "user_message", "content": "第二轮提问"},
            {"type": "llm_start"},
            {"type": "chunk", "content": "答案一"},
            {"type": "llm_end", "content": "答案二"},
            {"type": "done", "final_content": "答案二", "non_system_count": 4},
        ],
        "expect": ["答案一", "答案二"],
    },
    {
        # 迟到帧是长文本的**尾段**（真机形状：批量缓冲把末尾几段拼起来一次刷出）
        "name": "late_tail_chunk_into_next_round",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "上一轮答案的前半段，"},
            {"type": "llm_end", "content": "上一轮答案的前半段，以及最后的结尾句。"},
            {"type": "done", "final_content": "上一轮答案的前半段，以及最后的结尾句。", "non_system_count": 2},
            {"type": "user_message", "content": "第二轮提问"},
            {"type": "llm_start"},
            {"type": "llm_end", "content": "第二轮的完整答案"},
            {"type": "done", "final_content": "第二轮的完整答案", "non_system_count": 4},
            {"type": "chunk", "content": "以及最后的结尾句。"},
        ],
        "expect": ["上一轮答案的前半段，以及最后的结尾句。", "第二轮的完整答案"],
    },
    {
        # 假阳性护栏：新一轮首帧**恰好等于**上轮尾段（误判就会丢字）。
        # 允许丢弃（该帧终渲染由 llm_end 权威全文兜回），但**正文不得缺字**。
        "name": "new_round_first_chunk_equals_prev_tail",
        "events": [
            {"type": "llm_start"},
            {"type": "llm_end", "content": "上一轮以这句话收尾。"},
            {"type": "done", "final_content": "上一轮以这句话收尾。", "non_system_count": 2},
            {"type": "user_message", "content": "第二轮提问"},
            {"type": "llm_start"},
            {"type": "chunk", "content": "收尾。"},
            {"type": "chunk", "content": "这里是第二轮真正的内容"},
            {"type": "llm_end", "content": "收尾。这里是第二轮真正的内容"},
            {"type": "done", "final_content": "收尾。这里是第二轮真正的内容", "non_system_count": 4},
        ],
        "expect": ["上一轮以这句话收尾。", "收尾。这里是第二轮真正的内容"],
    },
    {
        # 迟到 thinking_content：不得让下一轮的思考块替上一轮记字
        "name": "late_thinking_after_next_round_start",
        "events": [
            {"type": "llm_start"},
            {"type": "llm_end", "content": "答案一"},
            {"type": "done", "final_content": "答案一", "non_system_count": 2},
            {"type": "user_message", "content": "第二轮提问"},
            {"type": "llm_start"},
            {"type": "thinking_content", "token": "上一轮的思考残留"},
            {"type": "chunk", "content": "答案二"},
            {"type": "llm_end", "content": "答案二"},
            {"type": "done", "final_content": "答案二", "non_system_count": 4},
        ],
        "expect": ["答案一", "答案二"],
    },
]

# ══════════════════════════════════════════════════════════════
# 三、重连 / 重同步竞态（真发 REST 请求，settle 放宽）
# ══════════════════════════════════════════════════════════════
RESYNC = [
    {
        "name": "resync_mid_turn",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终回复"},
            {"type": "connected", "resync": True, "processing": True, "run_id": "r2", "seq": 5},
            {"type": "chunk", "content": "正文"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
        ],
        "expect": ["最终回复正文"],
        "settle": 900,
    },
    {
        "name": "reconnect_mid_turn",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终回复"},
            {"type": "connected", "resync": False, "processing": True, "run_id": "r2", "seq": 5},
            {"type": "chunk", "content": "正文"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
        ],
        "expect": ["最终回复正文"],
        "settle": 900,
    },
    {
        "name": "history_changed_while_processing",
        "events": [
            {"type": "llm_start"},
            {"type": "chunk", "content": "最终回复正文"},
            {"type": "history_changed"},
            {"type": "llm_end", "content": "最终回复正文"},
            {"type": "done", "final_content": "最终回复正文", "non_system_count": 2},
        ],
        "expect": ["最终回复正文"],
        "settle": 900,
    },
]

ALL_CASES = SINGLE_TURN + CROSS_TURN + RESYNC


@pytest.mark.parametrize("case", ALL_CASES, ids=[c["name"] for c in ALL_CASES])
def test_no_duplicate_render(page, case):
    _run(page, case)
    _check(page, case)
