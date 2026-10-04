"""Zeta 发现层与实例锁测试（P1a）。

不依赖任何 codec / 网络——纯文件系统与领域模型。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from fp_core.plugins.zeta import discovery as disc
from fp_core.plugins.zeta.domain import PeerStatus, canonical_json


def _card(name: str, *, heartbeat: float | None = None, cwd: str = "/repo/x"):
    card = disc.build_card(name=name, cwd=cwd, business=f"{name} 的业务", tags=["backend"])
    if heartbeat is not None:
        card.heartbeat = heartbeat
        card.card_hash = card.compute_hash()
    return card


# ── 发现后端 ────────────────────────────────────────────


def test_publish_then_scan_roundtrip(tmp_path: Path):
    be = disc.SharedDirBackend(tmp_path)
    card = _card("alpha")
    be.publish(card)

    got = be.scan(now=time.time())
    assert [c.name for c in got] == ["alpha"]
    assert got[0].business == "alpha 的业务"
    assert got[0].tags == ["backend"]


def test_scan_filters_tampered_card(tmp_path: Path):
    be = disc.SharedDirBackend(tmp_path)
    be.publish(_card("alpha"))
    # 外部篡改内容但保留 hash → verify_card 失败 → 丢弃
    p = be.card_path("alpha")
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["business"] = "evil"
    p.write_text(canonical_json(raw), encoding="utf-8")
    assert be.scan(now=time.time()) == []


def test_scan_filters_stale_heartbeat(tmp_path: Path):
    be = disc.SharedDirBackend(tmp_path)
    be.publish(_card("alpha", heartbeat=time.time() - 1000))
    assert be.scan(now=time.time()) == []


def test_write_isolation_does_not_touch_others(tmp_path: Path):
    """实例 A 的 publish 不可能覆盖实例 B 的卡（§1.2 自写隔离）。"""
    be = disc.SharedDirBackend(tmp_path)
    be.publish(_card("bravo"))
    b_path = be.card_path("bravo")
    before = b_path.read_bytes()

    be.publish(_card("alpha"))

    assert b_path.read_bytes() == before, "写下 alpha 的卡不应改动 bravo 的卡"
    assert be.card_path("alpha").exists()


def test_withdraw_removes_only_own(tmp_path: Path):
    be = disc.SharedDirBackend(tmp_path)
    be.publish(_card("alpha"))
    be.publish(_card("bravo"))
    be.withdraw("alpha")
    names = [c.name for c in be.scan(now=time.time())]
    assert names == ["bravo"]


# ── 一 workspace 多实例（§1.5 修订）──────────────────────


def test_registry_allows_multiple_instances(tmp_path: Path):
    """B1：不同 name 的实例可共存于同一 workspace（记忆共享的前提）。"""
    a = disc.InstanceRegistry(tmp_path, "proj-aaaa1111")
    b = disc.InstanceRegistry(tmp_path, "proj-bbbb2222")
    a.acquire(sid="s_a")
    b.acquire(sid="s_b")  # 不冲突
    assert sorted(r["name"] for r in a.scan()) == ["proj-aaaa1111", "proj-bbbb2222"]


def test_registry_same_name_conflicts(tmp_path: Path):
    disc.InstanceRegistry(tmp_path, "proj-aaaa1111").acquire(sid="s_a")
    with pytest.raises(disc.InstanceConflictError):
        disc.InstanceRegistry(tmp_path, "proj-aaaa1111").acquire(sid="s_a")


def test_registry_stale_pid_is_swept(tmp_path: Path):
    """僵尸登记（pid 已死）→ sweep_stale 回收，不阻塞同名再占。"""
    reg = disc.InstanceRegistry(tmp_path, "proj-aaaa1111")
    reg.dir.mkdir(parents=True, exist_ok=True)
    reg.path.write_text(
        json.dumps({
            "name": "proj-aaaa1111",
            "sid": "s_old",
            "pid": 999999,
            "workspace": str(tmp_path),
            "started_at": 0,
        }),
        encoding="utf-8",
    )
    assert reg.sweep_stale() == 1
    assert reg.acquire(sid="s_new")["sid"] == "s_new"


def test_registry_single_mode_rejects_second(tmp_path: Path):
    """single 模式（保留旧语义）：任一活跃实例即冲突。"""
    disc.InstanceRegistry(tmp_path, "proj-aaaa1111", single=True).acquire(sid="s_a")
    with pytest.raises(disc.InstanceConflictError):
        disc.InstanceRegistry(tmp_path, "proj-bbbb2222", single=True).acquire(sid="s_b")


def test_registry_release_frees_name(tmp_path: Path):
    reg = disc.InstanceRegistry(tmp_path, "proj-aaaa1111")
    reg.acquire(sid="s_a")
    assert reg.read() is not None  # 占用事实
    reg.release()
    assert reg.read() is None  # 释放 = 登记文件删除（本进程 pid 存活，残留即失败）
    disc.InstanceRegistry(tmp_path, "proj-aaaa1111").acquire(sid="s_a")  # 释放后可再占


# ── 身份派生（§3.2，sid 单锚点）──────────────────────────


def test_derive_instance_name_stable_and_unique(tmp_path: Path):
    sid = "s_260606_173040621922"
    assert disc.derive_instance_name(tmp_path, sid) == disc.derive_instance_name(tmp_path, sid)
    assert disc.derive_instance_name(tmp_path, sid) != disc.derive_instance_name(tmp_path, "s_other")
    assert disc.derive_instance_name(tmp_path, sid).startswith(tmp_path.name)


def test_derive_instance_name_does_not_leak_sid(tmp_path: Path):
    sid = "s_260606_173040621922"
    assert sid not in disc.derive_instance_name(tmp_path, sid)


def test_check_session_workspace_flags_mismatch(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("FP_ZETA_STATE_DIR", str(tmp_path / "state"))
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    assert disc.check_session_workspace("s_x", a) is None  # 首次无错配
    assert disc.check_session_workspace("s_x", a) is None  # 同目录无错配
    assert disc.check_session_workspace("s_x", b) == str(a)  # 跨目录报告原 workspace


# ── 名片构造 ────────────────────────────────────────────


def test_build_card_is_self_consistent():
    card = disc.build_card(name="alpha", cwd="/repo/x", business="后端")
    assert card.card_hash == card.compute_hash()
    assert card.capabilities.profiles  # verify_card 要求
    assert card.status is PeerStatus.IDLE


def test_build_card_workspace_defaults_to_cwd():
    card = disc.build_card(name="alpha", cwd="/repo/x")
    assert card.workspace == "/repo/x"


def test_build_card_workspace_is_route_key():
    """路由键用 workspace（项目归属），不用 cwd。"""
    card = disc.build_card(name="alpha", cwd="/repo/x/sub", workspace="/repo/x", tags=["backend"], business="后端")
    assert card.workspace == "/repo/x"
    assert card.route_key() == (["backend"], "后端", "/repo/x")


def test_infer_business_from_readme(tmp_path: Path):
    (tmp_path / "README.md").write_text("# 项目\n\n这是 FP 平台开发。\n", encoding="utf-8")
    assert disc.infer_business(tmp_path) == "这是 FP 平台开发。"


def test_infer_business_skips_markup_noise(tmp_path: Path):
    (tmp_path / "README.md").write_text(
        '<div align="center">\n\n# FP\n\n**一个插件化 AI Agent 框架**\n\n[![Badge](x)](y)\n',
        encoding="utf-8",
    )
    # HTML 标签/徽章被跳过，描述行剥离 ** 标记
    assert disc.infer_business(tmp_path) == "一个插件化 AI Agent 框架"


def test_infer_business_falls_back_to_heading(tmp_path: Path):
    (tmp_path / "README.md").write_text("<div>\n\n# 只有标题的项目\n", encoding="utf-8")
    assert disc.infer_business(tmp_path) == "只有标题的项目"


def test_refresh_heartbeat_rehashes():
    card = _card("alpha", heartbeat=time.time() - 1000)
    disc.refresh_heartbeat(card, status=PeerStatus.BUSY)
    assert card.status is PeerStatus.BUSY
    assert card.card_hash == card.compute_hash()
