"""n5 双实例端到端：两个真实的 ZetaPlugin 实例在同一机互通。

这是「胜负手」的机器可验证部分——用两个**真实插件实例**（各自工作目录、
共享发现目录、真实注入队列）跑完整闭环：

    发现 → 名片路由 → propose → 对方机器校验 → ack → 契约落共享交换区

真实 LLM 参与的那半（A 自己决定找 B、B 自己决定接听）由人工实验执行；
本测试证明：**协议闭环无需 LLM 也成立**（LLM 只是决策者，不是信任根）。
"""

# 测试需要触碰被测对象的私有成员（模拟 core 的装配与轮次边界）。
# pyright: reportPrivateUsage=false, reportArgumentType=false, reportUnknownMemberType=false

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from fp_core.core import jobs
from fp_core.plugins.zeta import contracts as ct
from fp_core.plugins.zeta.plugin import ZetaPlugin


class FakeRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def register_tool(self, name: str, schema: Any, fn: Any) -> None:
        self.tools[name] = fn

    def unregister_tool(self, name: str) -> None:
        self.tools.pop(name, None)


class FakeCtx:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}


def _contract(contract_id: str = "ctr-01J") -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "id": contract_id,
        "name": "items-api",
        "revision": 1,
        "status": "proposed",
        "parties": [{"name": "A", "role": "backend"}, {"name": "B", "role": "frontend"}],
        "scope": {
            "resources": {
                "default_effect": "deny",
                "read": ["repo://server/src/**"],
                "write": ["repo://server/api/**"],
            }
        },
        "interfaces": [
            {"id": "get-items", "kind": "http", "http": {"method": "GET", "path": "/api/items"}},
        ],
    }


def test_two_instances_negotiate_contract_end_to_end(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    peers = tmp_path / "peers"
    monkeypatch.setenv("FP_ZETA_PEERS_DIR", str(peers))
    monkeypatch.delenv("FP_ZETA_DISABLE", raising=False)
    monkeypatch.delenv("FP_IS_SUBAGENT", raising=False)

    ws_a, ws_b = tmp_path / "ws-a", tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()

    async def _run() -> dict[str, Any]:
        a, b = ZetaPlugin(), ZetaPlugin()
        reg_a, reg_b = FakeRegistry(), FakeRegistry()

        monkeypatch.chdir(ws_a)
        await a._on_init(FakeCtx(), tool_registry=reg_a)
        monkeypatch.chdir(ws_b)
        await b._on_init(FakeCtx(), tool_registry=reg_b)

        assert a._service is not None and b._service is not None
        name_a, name_b = a._service.name, b._service.name

        # ① 发现：A 的邻居表里有 B（名片即路由）
        listing = await reg_a.tools["peer_list"]({})
        assert name_b in listing, f"未发现 B: {listing}"

        # ② A 发起契约提议（本地机器校验通过才出门）
        contract = _contract()
        propose_out = await reg_a.tools["peer_propose"]({"to": name_b, "contract": contract})
        assert "机器校验通过" in propose_out, propose_out

        # ③ B 收到铃声（真实注入队列，不含正文）
        jobs.drain_ready()
        b._service.poll()
        rings = jobs.drain_ready()
        assert any(m.get("kind") == "peer_ring" for m in rings), f"无铃声: {rings}"

        # ④ B 接听：正文 + 本地机器校验报告（双锁）
        answer = await reg_b.tools["peer_answer"]({})
        assert "[机器校验] 结构与安全区通过" in answer
        assert ct.contract_digest(contract)[:12] in answer

        # ⑤ B 回执（hash 锁字节）
        ack_out = await reg_b.tools["peer_ack"]({
            "to": name_a,
            "contract_id": "ctr-01J",
            "hash": ct.contract_digest(contract),
            "accept": True,
        })
        assert "接受" in ack_out

        # ⑥ A 收到回执（语义消息 → 铃声进真实注入队列）
        jobs.drain_ready()
        a._service.poll()
        acks = jobs.drain_ready()
        assert any(m.get("kind") == "peer_ring" for m in acks), f"A 未收到回执铃声: {acks}"

        a._stop()
        b._stop()
        await asyncio.sleep(0)
        return {"name_a": name_a, "name_b": name_b, "answer": answer, "propose": propose_out}

    result = asyncio.run(_run())

    # ⑦ 契约落到共享交换区（单写者：提议方 A 写，双方共读）
    exchange = ct.ContractExchange(peers / "contracts")
    assert exchange.exists("ctr-01J"), f"契约未落交换区: {list((peers / 'contracts').glob('*'))}"
    published = exchange.read("ctr-01J")
    assert ct.validate_contract(published) == []
    assert ct.contract_digest(published) == ct.contract_digest(_contract())
    assert result["name_a"] != result["name_b"]


def test_two_instances_offline_leave_message_then_deliver(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """邻居不在线 → 留话；对方上线后读到（不依赖同时启动）。"""
    peers = tmp_path / "peers"
    monkeypatch.setenv("FP_ZETA_PEERS_DIR", str(peers))
    monkeypatch.delenv("FP_ZETA_DISABLE", raising=False)

    ws_a, ws_b = tmp_path / "ws-a", tmp_path / "ws-b"
    ws_a.mkdir()
    ws_b.mkdir()

    async def _run() -> str:
        a = ZetaPlugin()
        reg_a = FakeRegistry()
        monkeypatch.chdir(ws_a)
        await a._on_init(FakeCtx(), tool_registry=reg_a)
        assert a._service is not None

        # B 尚未启动：A 不知道 B 的名字，用一个明确的名字留话
        out = await reg_a.tools["peer_send"]({"to": "ws-b", "topic": "chat", "body": "在吗"})
        assert "离线" in out

        a._stop()
        await asyncio.sleep(0)

        # B 上线（名字恰好是 ws-b 才收得到；此处直接验证留话文件已投递）
        return str(out)

    out = asyncio.run(_run())
    assert "离线" in out
    inbox = peers / "ws-b.inbox"
    assert inbox.exists() and any(inbox.glob("msg-*.json")), "留话未落到收件箱"
