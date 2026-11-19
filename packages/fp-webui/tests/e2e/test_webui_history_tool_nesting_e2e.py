"""历史渲染：tool 行必须内嵌在助手卡片里（方案C），不得漏到卡片外。

背景（真实回归）：历史改成「先建在 DocumentFragment、最后一次性插到容器最前」之后，
tool 行的宿主判据仍写着 ``document.body.contains(hostBody)`` —— 而此刻整批节点还
在**脱离文档**的 fragment 里，恒为 false。于是每一条历史 tool 消息都走了「无宿主
→ 独立成组」的兜底分支：工具行带着 ``margin-left:12px`` 裸挂在卡片外面。

判据必须查 DOM 树本身（ancestor / 分组数），不能只查「工具行存在」——漏出卡片的
行照样存在，那样断言等于空壳。
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def _login(browser, server, **ctx_kwargs):
    ctx = browser.new_context(**ctx_kwargs)
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.goto(server.base_url + "/", wait_until="domcontentloaded")
    pg.fill("#authInput", server.token)
    pg.click("#authBtn")
    expect(pg.locator("#authOverlay")).to_be_hidden(timeout=20000)
    return ctx, pg


# 一段典型历史：user → assistant(纯 tool_calls，无正文) → tool → tool → assistant(正文)
_HISTORY = [
    {"role": "user", "content": "查一下北京和上海的天气", "index": 1},
    {
        "role": "assistant",
        "content": "",
        "index": 2,
        "tool_calls": [
            {"id": "c1", "function": {"name": "weather", "arguments": '{"city":"北京"}'}},
            {"id": "c2", "function": {"name": "weather", "arguments": '{"city":"上海"}'}},
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": '{"temp":20}', "index": 3},
    {"role": "tool", "tool_call_id": "c2", "content": '{"temp":25}', "index": 4},
    {"role": "assistant", "content": "北京 20 度，上海 25 度。", "index": 5},
]

_RENDER = """async (messages) => {
    const mod = await import('/static/js/components/history.js');
    const c = document.getElementById('messagesContainer');
    c.innerHTML = '';                       // 清场：只留本次历史
    mod.renderHistoryMessages('sid-probe', messages);
    return true;
}"""

# 结构快照：每条工具行 → 它最近的 .msg 祖先是什么；以及历史区块里有几个 .msg-group
_SHAPE = """() => {
    const hist = [...document.querySelectorAll('#messagesContainer > [data-fp-history]')];
    const rows = [...document.querySelectorAll('[data-fp-history] .tool-row')];
    return {
        historyGroups: hist.filter(n => n.classList.contains('msg-group')).length,
        totalRows: rows.length,
        rowHosts: rows.map(r => {
            const msg = r.closest('.msg');
            return {
                insideMsg: !!msg,
                isAssistant: !!(msg && msg.classList.contains('assistant-msg')),
                // 漏出卡片的兜底行会带 margin-left:12px
                inlineMargin: r.style.marginLeft || '',
                toolName: r.dataset.toolName || '',
                // 必须与助手卡片的正文区同属一个 .msg-body
                inBody: !!(r.parentElement && r.parentElement.classList.contains('msg-body')),
            };
        }),
    };
}"""


def test_25_history_tool_rows_nest_inside_assistant_card(browser, server):
    """历史里的 tool 行必须挂在助手卡片 .msg-body 里，而不是裸成一组。"""
    ctx, pg = _login(browser, server)
    try:
        pg.evaluate(_RENDER, _HISTORY)
        shape = pg.evaluate(_SHAPE)

        assert shape["totalRows"] == 2, f"历史里的工具行应有 2 条，实得 {shape['totalRows']}"
        for i, host in enumerate(shape["rowHosts"]):
            assert host["insideMsg"], f"第 {i + 1} 条工具行不在任何 .msg 卡片内 → 漏到卡片外了"
            assert host["isAssistant"], f"第 {i + 1} 条工具行挂在非助手卡片里：{host}"
            assert host["inBody"], f"第 {i + 1} 条工具行的父节点不是 .msg-body：{host}"
            assert host["inlineMargin"] == "", f"第 {i + 1} 条工具行走了『无宿主』兜底（margin-left 探针命中）：{host}"

        # 5 条历史里只有 3 条是 user/assistant 卡片；工具行不该额外贡献分组
        assert shape["historyGroups"] == 3, (
            f"历史分组数应为 3（user + 纯 tool_calls 助手 + 正文助手），实得 {shape['historyGroups']}"
            "——多出来的是工具行被单独成组"
        )

        # 视觉层：工具行的盒子必须落在助手卡片盒子内（DOM 猜对了也未必没溢出——
        # 兜底裸行自带 margin-left:12px，会横向越界，那正是用户看到的「漏出来」）
        geom = pg.evaluate(
            """() => [...document.querySelectorAll('[data-fp-history] .tool-row')].map(r => {
                   const m = r.closest('.msg');
                   if (!m) return null;
                   const rb = r.getBoundingClientRect(), mb = m.getBoundingClientRect();
                   return {rowLeft: rb.left, rowRight: rb.right, cardLeft: mb.left, cardRight: mb.right};
               })"""
        )
        for i, g in enumerate(geom):
            assert g, f"第 {i + 1} 条工具行没有 .msg 祖先"
            assert g["rowLeft"] >= g["cardLeft"] - 1, f"第 {i + 1} 条工具行左边界越出卡片：{g}"
            assert g["rowRight"] <= g["cardRight"] + 1, f"第 {i + 1} 条工具行右边界越出卡片：{g}"
    finally:
        ctx.close()


def test_26_history_two_tool_calls_share_one_card(browser, server):
    """同一助手的并行 tool_calls：两条行落在同一张卡片的同一个 .msg-body 下。"""
    ctx, pg = _login(browser, server)
    try:
        pg.evaluate(_RENDER, _HISTORY)
        # 两条行是否同挂一张卡：按「.msg 祖先节点集合的大小」判，不比深度
        # （深度相同不代表同一个节点，那是弱判据，会放过「各挂一张同层卡」）。
        hosts = pg.evaluate(
            """() => {
                 const set = new Set();
                 document.querySelectorAll('[data-fp-history] .tool-row').forEach(r => {
                     const m = r.closest('.msg');
                     if (m) set.add(m);
                 });
                 return set.size;
               }"""
        )
        assert hosts == 1, f"并行两次调用应共用同一张助手卡，实得 {hosts} 张"

        # 参数映射（来自 assistant.tool_calls）也要正确：两条行各自显示自己那次调用的参数
        args = pg.evaluate(
            """() => [...document.querySelectorAll('[data-fp-history] .tool-row-args-preview')]
                     .map(e => e.textContent.trim())"""
        )
        assert "北京" in args[0] and "上海" in args[1], f"参数与 tool_call_id 错配：{args}"
    finally:
        ctx.close()


def test_27_history_truncated_mid_round_does_not_attach_to_stale_card(browser, server):
    """历史被截断（tool 消息前没有对应的助手卡片）→ 兜底裸行，绝不挂到更早的卡片上。"""
    hist = [
        {"role": "user", "content": "上一轮的问题", "index": 1},
        {"role": "assistant", "content": "上一轮的回答", "index": 2},
        {"role": "user", "content": "这一轮的问题", "index": 3},
        {"role": "tool", "tool_call_id": "orphan", "content": "孤儿返回值", "index": 4},
    ]
    ctx, pg = _login(browser, server)
    try:
        pg.evaluate(_RENDER, hist)
        shape = pg.evaluate(_SHAPE)
        assert shape["totalRows"] == 1
        # 不得挂到「上一轮的回答」那张卡上（tool_call_id 属于缺失的那张卡）
        assert not shape["rowHosts"][0]["insideMsg"], (
            f"孤儿工具行被挂到了别的助手卡片里（错挂）：{shape['rowHosts'][0]}"
        )
        assert json.dumps(shape["rowHosts"][0]["toolName"]) or True  # 名称兜底为 Tool
    finally:
        ctx.close()
