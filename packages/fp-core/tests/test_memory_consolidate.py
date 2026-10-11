"""测试 P4 巩固检测 — memoryidate（机检信号 + flock 单写者 + 日门控 + journal/报告）

设计：/tmp/fp_memory_tree_design_20261009.md §3/§8
- detect()：split / merge / near_dup / orphan / misfile / forbidden 全信号（零 LLM）
- run()：日门控（同日只跑一次，force 绕过）、flock 单写者（另一实例持锁则让位）、
  journal jsonl 追加、dated 报告 md 落盘
- 插件 memory 只接线不测逻辑（ON_INITIALIZED → run，异常不阻塞启动）
"""

import json
import os
from datetime import date, datetime, timedelta

import pytest

from fp_core import config
from fp_core.platform_utils import FileLock
from fp_core.plugins.memory import consolidate as mc
from fp_core.plugins.memory import tree as memory_tree


@pytest.fixture
def memory_env(tmp_path, monkeypatch):
    """隔离记忆目录 + 巩固日志路径"""
    data_dir = tmp_path / "fpdata"
    monkeypatch.setattr(config, "_FP_DATA_DIR", str(data_dir))
    monkeypatch.chdir(tmp_path)

    jdir = tmp_path / "journal"
    jdir.mkdir()
    monkeypatch.setattr(mc, "JOURNAL_DIR", str(jdir))
    monkeypatch.setattr(mc, "LOCK_PATH", str(jdir / "memoryidate.lock"))
    monkeypatch.setattr(mc, "GATE_PATH", str(jdir / "memoryidate.gate"))
    monkeypatch.setattr(mc, "JOURNAL_PATH", str(jdir / "memoryidate.jsonl"))

    return {"global": str(data_dir / "private" / "memory"), "journal": str(jdir)}


def _write(
    mem_root: str, category: str, name: str, description: str, created: str | None = None, type_: str | None = None
):
    d = os.path.join(mem_root, category)
    os.makedirs(d, exist_ok=True)
    cr = created or datetime.now().strftime("%Y-%m-%d %H:%M")
    with open(os.path.join(d, f"{name}.md"), "w", encoding="utf-8") as f:
        f.write("---\n")
        f.write(f"name: {name}\n")
        f.write(f"description: {description}\n")
        f.write(f"type: {type_ or category}\n")
        f.write(f"created: {cr}\n")
        f.write(f"updated: {cr}\n")
        f.write("context: 测试用途\n")
        f.write("---\n\n正文\n")


def test_detect_all_signals(memory_env):
    g = memory_env["global"]

    # split：21 条 > 20
    for i in range(21):
        _write(g, "project", f"proj{i:02d}", f"项目任务编号 {i} 的进展记录条目")
    # merge：2 条 < 4
    _write(g, "user", "u1", "用户偏好记录一")
    _write(g, "user", "u2", "用户偏好记录二")
    # near_dup：description 几乎相同（跨分类）
    _write(g, "skill", "dup_a", "hyperframes 挂载 recipe：步骤与常见坑")
    _write(g, "reference", "dup_b", "hyperframes 挂载 recipe：步骤与常见坑")
    # orphan：created 100 天前 + 零访问
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d %H:%M")
    _write(g, "feedback", "old_one", "一条久未访问的旧反馈", created=old)
    # misfile：目录 skill 里 frontmatter type=reference
    _write(g, "skill", "wrong_drawer", "放错抽屉的条目", type_="reference")
    # forbidden：禁用分类目录
    _write(g, "misc", "junk", "杂项不该存在")

    f = mc.detect("~")

    assert f["total"] == 21 + 2 + 2 + 1 + 1 + 1
    assert any(x["category"] == "project" and x["count"] == 21 for x in f["split"])
    assert any(x["category"] == "user" for x in f["merge"])
    assert any({x["a"], x["b"]} == {"skill/dup_a", "reference/dup_b"} for x in f["near_dup"])
    assert any(x["name"] == "old_one" for x in f["orphan"])
    assert any(
        x["name"] == "wrong_drawer" and x["suggest"] == "skill/wrong_drawer → reference/wrong_drawer"
        for x in f["misfile"]
    )
    assert any(x["category"] == "misc" for x in f["forbidden"])


def test_detect_not_orphan_when_accessed(memory_env):
    g = memory_env["global"]
    old = (datetime.now() - timedelta(days=100)).strftime("%Y-%m-%d %H:%M")
    _write(g, "reference", "hot_old", "虽旧但常被读取", created=old)
    memory_tree.bump_access("~", "hot_old")  # 访问计数 > 0

    f = mc.detect("~")
    assert not any(x["name"] == "hot_old" for x in f["orphan"])


def test_run_gate_daily(memory_env):
    g = memory_env["global"]
    _write(g, "skill", "s1", "技能条目一")

    first = mc.run()
    assert "巩固检测完成" in first
    second = mc.run()
    assert "日门控" in second
    third = mc.run(force=True)
    assert "巩固检测完成" in third


def test_run_flock_single_writer(memory_env):
    g = memory_env["global"]
    _write(g, "skill", "s1", "技能条目一")

    # 另一实例（此处用第二个 FileLock 具象化）持锁 → 单写者互斥成立
    holder = FileLock(mc.LOCK_PATH)
    assert holder.acquire(), "测试前置：holder 应能拿到锁"
    try:
        out = mc.run(force=True)
        assert "单写者" in out and "跳过" in out
    finally:
        holder.release()

    out2 = mc.run(force=True)
    assert "巩固检测完成" in out2


def test_run_writes_journal_and_report(memory_env):
    g = memory_env["global"]
    _write(g, "project", "p1", "项目条目")
    _write(g, "project", "p2", "项目条目")  # 近重对
    _write(g, "misc", "junk", "禁用分类")  # forbidden

    mc.run(force=True)

    # journal jsonl：结构化逐条
    with open(mc.JOURNAL_PATH, encoding="utf-8") as fp:
        entries = [json.loads(line) for line in fp if line.strip()]
    kinds = {e["kind"] for e in entries}
    assert "near_dup" in kinds
    assert "forbidden" in kinds
    assert all(e["ts"] and e["root"] for e in entries)

    # dated 报告 md
    report = mc._report_path(date.today().isoformat())
    assert os.path.isfile(report)
    with open(report, encoding="utf-8") as fp:
        content = fp.read()
    assert "近重" in content and "禁用" in content


def test_plugin_registered():
    """插件包导出 Plugin 子类（扫描加载的契约）"""
    from fp_core.plugins.base.plugin import Plugin
    from fp_core.plugins.memory import MemoryPlugin

    assert issubclass(MemoryPlugin, Plugin)
    assert MemoryPlugin.name == "memory"
