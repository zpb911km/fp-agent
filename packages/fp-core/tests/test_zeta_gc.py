"""共享发现目录卫生（可达性 GC，方案 B）。

设计红线：
- 删名片要**同时**满足「心跳过期 > grace」+「其 workspace 下无同名活登记」，
  判不了就留（宁留勿删）；名片是派生缓存，活实例每 2s 重发，误删也会自愈。
- 收件箱**顶层有未读留话 → 永不删**：那是给未来自己的话（名字由 sid 派生，
  会话 resume 即同名、上线即读到），删了就永久丢消息。
- 多实例并发经 flock 互斥，拿不到锁 → 跳过（不是失败，也不算删了东西）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from fp_core.plugins.zeta import discovery as disc
from fp_core.plugins.zeta.discovery import SharedDirBackend, build_card, collect_garbage, instance_alive
from fp_core.plugins.zeta.transport import DirTransport

DAY = 86400.0


# ── 造场景的小工具 ─────────────────────────────────────


def _publish(root: Path, name: str, *, workspace: Path | None = None, age: float = 0.0) -> None:
    """发一张心跳龄 = age 秒的名片。"""
    ws = workspace or root
    card = build_card(name=name, cwd=ws, workspace=ws, business=name)
    card.heartbeat = time.time() - age
    card.card_hash = card.compute_hash()
    SharedDirBackend(root).publish(card)


def _register(workspace: Path, name: str, *, pid: int) -> None:
    """在 workspace 下写一条实例登记（= 该实例此刻活着）。"""
    d = workspace / ".fp" / "zeta" / "instances"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.json").write_text(
        json.dumps({"name": name, "sid": "s", "pid": pid, "workspace": str(workspace)}), encoding="utf-8"
    )


def _inbox(root: Path, name: str, *, mail: int = 0, processed: int = 0) -> Path:
    """造一个收件箱：mail 条未读留话 + processed 条历史。"""
    d = DirTransport(root).inbox_dir(name)
    (d / ".processed").mkdir(parents=True, exist_ok=True)
    for i in range(mail):
        (d / f"msg-unread{i}.json").write_text("{}", encoding="utf-8")
    for i in range(processed):
        (d / ".processed" / f"msg-old{i}.json").write_text("{}", encoding="utf-8")
    return d


def _live_foreign_pid() -> tuple[int, subprocess.Popen[bytes]]:
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    return proc.pid, proc


# ── 名片：两条必要判据 ─────────────────────────────────


def test_dead_card_is_reclaimed(tmp_path: Path):
    """心跳过期 >grace 且 workspace 下无活登记 → 回收。"""
    root = tmp_path / "peers"
    ws = tmp_path / "gone"  # workspace 已不存在
    _publish(root, "proj-dead", workspace=ws, age=8 * DAY)

    rep = collect_garbage(SharedDirBackend(root))

    assert rep.cards == 1
    assert not (root / "proj-dead.json").exists()


def test_fresh_card_is_kept(tmp_path: Path):
    root = tmp_path / "peers"
    _publish(root, "proj-live", workspace=tmp_path, age=0)

    rep = collect_garbage(SharedDirBackend(root))

    assert rep.cards == 0
    assert (root / "proj-live.json").exists()


def test_card_within_grace_is_kept(tmp_path: Path):
    """过期但未过 grace（例如刚被 kill 一小时）→ 先留，别急着删。"""
    root = tmp_path / "peers"
    _publish(root, "proj-recent", workspace=tmp_path / "gone", age=3600)

    assert collect_garbage(SharedDirBackend(root)).cards == 0
    assert (root / "proj-recent.json").exists()


def test_card_with_live_registry_is_kept(tmp_path: Path):
    """心跳过期 >7 天但登记里的 pid 还活着（实例卡住/挂起）→ 保守保留。"""
    root = tmp_path / "peers"
    ws = tmp_path / "ws"
    ws.mkdir()
    pid, proc = _live_foreign_pid()
    try:
        _register(ws, "proj-stuck", pid=pid)
        _publish(root, "proj-stuck", workspace=ws, age=8 * DAY)

        assert collect_garbage(SharedDirBackend(root)).cards == 0
        assert (root / "proj-stuck.json").exists()
        assert instance_alive(ws, "proj-stuck") is True
    finally:
        proc.kill()
        proc.wait()


def test_card_kept_when_registry_undecidable(tmp_path: Path):
    """回查读不了（判不了）→ 当"还在"处理，不删。"""
    root = tmp_path / "peers"
    ws = tmp_path / "ws"
    (ws / ".fp" / "zeta" / "instances").mkdir(parents=True)
    (ws / ".fp" / "zeta" / "instances" / "proj-bad.json").write_text("{半写", encoding="utf-8")
    _publish(root, "proj-bad", workspace=ws, age=8 * DAY)

    assert collect_garbage(SharedDirBackend(root)).cards == 0
    assert (root / "proj-bad.json").exists()


def test_corrupt_card_reclaimed_after_grace(tmp_path: Path):
    """损坏名片（无可回查的 workspace）→ 按文件龄，过期才回收。"""
    root = tmp_path / "peers"
    root.mkdir(parents=True)
    bad = root / "junk.json"
    bad.write_text("{半写", encoding="utf-8")
    old = time.time() - 8 * DAY
    os.utime(bad, (old, old))

    assert collect_garbage(SharedDirBackend(root)).cards == 1
    assert not bad.exists()


# ── 收件箱：留话是红线 ─────────────────────────────────


def test_inbox_with_unread_mail_is_never_removed(tmp_path: Path):
    """有未读留话的收件箱——哪怕名片早没了、例子里的"投递黑洞"——也永不删。"""
    root = tmp_path / "peers"
    d = _inbox(root, "proj-blackhole", mail=2, processed=3)

    rep = collect_garbage(SharedDirBackend(root))

    assert rep.inboxes == 0
    assert rep.kept_with_mail == 1
    assert d.is_dir()
    assert len(list(d.glob("msg-*.json"))) == 2


def test_dead_inbox_without_mail_is_removed(tmp_path: Path):
    """无未读 + 名片已死 → 整目录回收（含死实例永无人清的 .processed）。"""
    root = tmp_path / "peers"
    _publish(root, "proj-dead", workspace=tmp_path / "gone", age=8 * DAY)
    d = _inbox(root, "proj-dead", mail=0, processed=5)

    rep = collect_garbage(SharedDirBackend(root))

    assert rep.inboxes == 1
    assert not d.exists()


def test_live_inbox_is_kept(tmp_path: Path):
    """活实例的收件箱：名片新鲜 → 不归 GC 管（由它自己的 housekeeping 清）。"""
    root = tmp_path / "peers"
    _publish(root, "proj-live", workspace=tmp_path, age=0)
    d = _inbox(root, "proj-live", mail=0, processed=2)

    assert collect_garbage(SharedDirBackend(root)).inboxes == 0
    assert d.is_dir()


# ── 并发、dry_run、报告 ────────────────────────────────


def test_dry_run_reports_without_touching(tmp_path: Path):
    root = tmp_path / "peers"
    _publish(root, "proj-dead", workspace=tmp_path / "gone", age=8 * DAY)
    d = _inbox(root, "proj-dead", mail=0, processed=1)

    rep = collect_garbage(SharedDirBackend(root), dry_run=True)

    assert (rep.cards, rep.inboxes) == (1, 1)
    assert rep.bytes_freed > 0
    assert (root / "proj-dead.json").exists() and d.is_dir()


def test_skips_when_another_instance_holds_the_lock(tmp_path: Path):
    root = tmp_path / "peers"
    _publish(root, "proj-dead", workspace=tmp_path / "gone", age=8 * DAY)
    lock = disc.FileLock(str(root / disc.GC_LOCK_NAME))
    assert lock.acquire()
    try:
        rep = collect_garbage(SharedDirBackend(root))
        assert rep.skipped_by_lock is True
        assert (root / "proj-dead.json").exists()  # 一根手指都没动
    finally:
        lock.release()

    # 锁释放后照常干活
    assert collect_garbage(SharedDirBackend(root)).cards == 1


def test_report_summary_and_bool(tmp_path: Path):
    assert bool(disc.GcReport()) is False
    rep = disc.GcReport(cards=2, inboxes=1, bytes_freed=2048, kept_with_mail=3)
    assert bool(rep) is True
    assert "回收名片 2 张" in rep.summary()
    assert "2.0 KB" in rep.summary()
    assert "保留 3 个" in rep.summary()
    assert "跳过" in disc.GcReport(skipped_by_lock=True).summary()


def test_missing_dir_is_noop(tmp_path: Path):
    rep = collect_garbage(SharedDirBackend(tmp_path / "nope"))
    assert not rep and rep.bytes_freed == 0


# ── 插件侧接线 ─────────────────────────────────────────


def test_plugin_runs_gc_once_per_interval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """服务循环里启动即跑一次，之后按 24h 节流（不每轮 poll 都扫）。"""
    from fp_core.plugins.zeta import plugin as plugin_mod

    calls: list[float] = []
    monkeypatch.setattr(plugin_mod, "collect_garbage", lambda *a, **kw: calls.append(time.time()))
    monkeypatch.setenv("FP_ZETA_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("FP_ZETA_STATE_DIR", str(tmp_path / "state"))

    p = plugin_mod.ZetaPlugin()
    p._backend = SharedDirBackend(tmp_path / "peers")

    p._maybe_gc()
    p._maybe_gc()  # 节流内
    assert len(calls) == 1

    p._last_gc = time.time() - 25 * 3600  # 假装过了 24h
    p._maybe_gc()
    assert len(calls) == 2


def test_plugin_gc_failure_does_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """卫生任务炸了也不能影响邻居面。"""
    from fp_core.plugins.zeta import plugin as plugin_mod

    def boom(*a: object, **kw: object) -> None:
        raise OSError("磁盘出问题")

    monkeypatch.setattr(plugin_mod, "collect_garbage", boom)
    p = plugin_mod.ZetaPlugin()
    p._maybe_gc()  # 不抛
