"""zeta 的时间观念与回执自律 —— ``alarm`` 与 ``peer_send(require=True)``。

设计约束（2026-11 形忙实等）：
- **无跨进程机制**：两者都只走本地泵（``jobs.inject_event``）与已有共享注册表；
- ``alarm`` = 自己给自己的本地定时器（不精确、不跨进程、随进程亡）；
- ``require`` = **发件方自律**：收信方看不见、也不该看见，故不进信封正文；
  判据是"对方当前 idle 且未回音"——可观测状态，**不判死活**；
- 单次边沿触发（不持续催），且需 grace，避免"对方刚要回就被判忘"。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fp_core.core import jobs as jobs_impl
from fp_core.plugins.zeta.discovery import build_card
from fp_core.plugins.zeta.domain import PeerStatus
from fp_core.plugins.zeta.plugin import ALARM_TOOL, PEER_SEND_TOOL, ZetaPlugin


@pytest.fixture(autouse=True)
def _clean_pump():
    jobs_impl.pending_inject.clear()
    yield
    jobs_impl.pending_inject.clear()


class FakeBackend:
    """只实现 scan —— require 判定所需的唯一外部事实。"""

    def __init__(self, cards=None) -> None:
        self.cards = list(cards or [])

    def scan(self, now=None):  # noqa: ANN001, ANN201
        return list(self.cards)

    def publish(self, card) -> None:  # noqa: ANN001
        for i, c in enumerate(self.cards):
            if c.name == card.name:
                self.cards[i] = card
                return
        self.cards.append(card)

    def withdraw(self, name: str) -> None:  # noqa: ANN001
        self.cards = [c for c in self.cards if c.name != name]


class FakeService:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str]] = []

    def send(self, to: str, body: str, topic: str = "chat"):
        self.sent.append((to, body, topic))
        return "msg-1", True

    def last_inbound_from(self, name: str) -> float:
        return 0.0


def _plugin(backend=None) -> ZetaPlugin:
    p = ZetaPlugin()
    p._backend = backend or FakeBackend()
    p._service = FakeService()
    return p


def _idle_card(name: str = "w1"):
    return build_card(name=name, cwd="/tmp/w1", workspace="/tmp/w1", status=PeerStatus.IDLE)


def _busy_card(name: str = "w1"):
    return build_card(name=name, cwd="/tmp/w1", workspace="/tmp/w1", status=PeerStatus.BUSY)


# ── alarm ────────────────────────────────────────────


def test_alarm_schema_is_local_timer():
    fn = ALARM_TOOL["function"]
    assert fn["name"] == "alarm"
    props = fn["parameters"]["properties"]
    assert "delay" in props and "text" in props
    assert set(fn["parameters"]["required"]) == {"delay", "text"}
    # 语义必须写明"本地、不精确、走空闲泵"——否则 LLM 会当成跨进程定时器
    assert "本地" in fn["description"]


@pytest.mark.asyncio
async def test_alarm_fires_into_pump():
    p = _plugin()
    out = json.loads(await p._exec_alarm({"delay": 0.01, "text": "该看工人了"}))
    assert out["status"] == "scheduled"

    await asyncio.sleep(0.05)
    items = list(jobs_impl.pending_inject)
    assert [i.kind for i in items] == ["alarm"]
    assert items[0].wake is True, "闹钟必须能起一轮（否则等于没设）"
    assert "该看工人了" in items[0].content


@pytest.mark.asyncio
async def test_alarm_rejects_bad_params():
    p = _plugin()
    for bad in ({}, {"delay": 1}, {"text": "x"}, {"delay": "abc", "text": "x"}, {"delay": -5, "text": "x"}):
        out = json.loads(await p._exec_alarm(bad))
        assert out["status"] == "error", bad
    assert not jobs_impl.pending_inject


@pytest.mark.asyncio
async def test_alarm_clamped_upper_bound():
    p = _plugin()
    out = json.loads(await p._exec_alarm({"delay": 10**9, "text": "x"}))
    assert out["status"] == "scheduled"
    assert out["delay"] <= p._alarm_max
    for t in list(p._alarms):
        t.cancel()


@pytest.mark.asyncio
async def test_alarm_cancelled_on_stop():
    p = _plugin()
    await p._exec_alarm({"delay": 60, "text": "很久以后"})
    assert len(p._alarms) == 1
    task = next(iter(p._alarms))
    p._stop_alarms()
    assert not p._alarms
    await asyncio.sleep(0.01)  # 取消是异步的：cancel() 后需一个 tick 才落到 cancelled()
    assert task.cancelled() or task.done(), "被取消的闹钟必须真的终止"
    assert not jobs_impl.pending_inject, "被取消的闹钟不得再注入"


# ── require ──────────────────────────────────────────


def test_peer_send_schema_has_require_flag():
    props = PEER_SEND_TOOL["function"]["parameters"]["properties"]
    assert "require" in props
    assert props["require"]["type"] == "boolean"
    # 描述必须说清它管的是"我自己监视"，不是"强制对方回"
    assert "自己" in props["require"]["description"]


@pytest.mark.asyncio
async def test_require_only_registers_when_true():
    p = _plugin()
    await p._exec_peer_send({"to": "w1", "body": "进度如何"})
    assert p._requires == []
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    assert len(p._requires) == 1
    assert p._requires[0]["to"] == "w1"


@pytest.mark.asyncio
async def test_require_not_in_envelope_body():
    """收信方看不见 require —— 它不得出现在投递正文/主题里。"""
    p = _plugin()
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    to, body, topic = p._service.sent[0]
    assert (to, body, topic) == ("w1", "进度如何", "chat")
    assert "require" not in body


@pytest.mark.asyncio
async def test_require_reminds_once_when_target_idle():
    p = _plugin(FakeBackend([_idle_card()]))
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    p._requires[0]["sent_at"] -= p._require_grace + 5  # 已过 grace

    p._check_requires()
    items = [i for i in jobs_impl.pending_inject if i.kind == "peer_no_reply"]
    assert len(items) == 1
    assert items[0].wake is True
    assert "w1" in items[0].content

    jobs_impl.pending_inject.clear()
    p._check_requires()
    assert not [i for i in jobs_impl.pending_inject if i.kind == "peer_no_reply"], "单次边沿触发，不得持续催"


@pytest.mark.asyncio
async def test_require_silent_when_target_busy():
    p = _plugin(FakeBackend([_busy_card()]))
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    p._requires[0]["sent_at"] -= p._require_grace + 5
    p._check_requires()
    assert not jobs_impl.pending_inject, "对方在忙（含正在 wait）→ 不提醒；它随时可被电话唤醒"


@pytest.mark.asyncio
async def test_require_silent_within_grace():
    p = _plugin(FakeBackend([_idle_card()]))
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    p._check_requires()
    assert not jobs_impl.pending_inject, "刚发出就判忘 = 误报"


@pytest.mark.asyncio
async def test_require_silent_when_peer_answered():
    p = _plugin(FakeBackend([_idle_card()]))
    p._service.last_inbound_from = lambda name: 1e12  # type: ignore[method-assign]
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    p._requires[0]["sent_at"] -= p._require_grace + 5
    p._check_requires()
    assert not jobs_impl.pending_inject, "已回音 → 清账"


@pytest.mark.asyncio
async def test_require_reports_when_target_went_offline():
    """发送时在线、之后离线 → **必须报一次**。

    否则"对方被系统性错误干掉、我还在等"这类事故永远无人发现。
    但措辞只陈述观测（不在线），不下"它死了"的判决。
    """
    backend = FakeBackend([_idle_card()])
    p = _plugin(backend)
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    assert p._requires[0]["was_online"] is True
    p._requires[0]["sent_at"] -= p._require_grace + 5
    backend.cards = []  # 心跳过期 → scan 过滤掉

    p._check_requires()
    items = [i for i in jobs_impl.pending_inject if i.kind == "peer_no_reply"]
    assert len(items) == 1
    assert items[0].wake is True
    assert "w1" in items[0].content and "已下线" in items[0].content
    for verdict in ("死了", "崩溃", "挂了", "终止"):
        assert verdict not in items[0].content, f"只陈述观测，不得判死（出现「{verdict}」）"

    jobs_impl.pending_inject.clear()
    p._check_requires()
    assert not [i for i in jobs_impl.pending_inject if i.kind == "peer_no_reply"], "离线也只提醒一次"


@pytest.mark.asyncio
async def test_require_offline_silent_within_grace():
    """离线也不能抢跑：宽限内不报，避免对方短暂重启被误报。"""
    p = _plugin(FakeBackend([]))
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    p._check_requires()
    assert not jobs_impl.pending_inject


@pytest.mark.asyncio
async def test_require_reports_never_online():
    """发送时就没上线过 → 报「始终未上线」（留话可能仍躺在对方收件箱）。"""
    p = _plugin(FakeBackend([]))
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    p._requires[0]["was_online"] = False  # FakeService 固定报在线，此处显式构造该情形
    p._requires[0]["sent_at"] -= p._require_grace + 5
    p._check_requires()
    items = [i for i in jobs_impl.pending_inject if i.kind == "peer_no_reply"]
    assert len(items) == 1
    assert "始终未上线" in items[0].content


# ── 状态发布（require 的唯一硬依赖）──────────────────


def test_busy_flag_drives_published_status():
    p = _plugin()
    p._card = _idle_card("me")
    p._busy = True
    assert p._published_status() is PeerStatus.BUSY
    p._busy = False
    assert p._published_status() is PeerStatus.IDLE


@pytest.mark.asyncio
async def test_busy_hooks_toggle_flag():
    from fp_core.core.lifecycle import HookContext, LifecycleHook

    p = _plugin()
    ctx = HookContext(hook=LifecycleHook.ON_INPUT, data={})
    await p._hook_busy(ctx)
    assert p._busy is True
    await p._hook_idle(ctx)
    assert p._busy is False


@pytest.mark.asyncio
async def test_require_waits_until_peer_returns_idle():
    """对方忙 → 不提醒；回到 idle → 提醒一次（边沿）。"""
    backend = FakeBackend([_busy_card()])
    p = _plugin(backend)
    await p._exec_peer_send({"to": "w1", "body": "进度如何", "require": True})
    p._requires[0]["sent_at"] -= p._require_grace + 5
    p._check_requires()
    assert not jobs_impl.pending_inject

    backend.cards = [_idle_card()]
    p._check_requires()
    assert [i.kind for i in jobs_impl.pending_inject] == ["peer_no_reply"]
