"""Task System(任务图)插件测试

覆盖: 模型 / 旧格式迁移 / 原子写 / 图 op(含原子回滚) / delta 协议(解析+8校验) /
渲染 / 6 工具 / 插件钩子。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest

# ── 确保 src 在 sys.path ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_core.plugins.base.plugin import PluginRegistry
from fp_core.plugins.task_system import TaskSystemPlugin, tools
from fp_core.taskmap import delta as delta_mod
from fp_core.taskmap import render
from fp_core.taskmap.graph import GraphOpError, apply_ops
from fp_core.taskmap.models import MapStatus, NodeStatus, TaskMap
from fp_core.taskmap.store import TaskMapStore
from fp_core.tools import ToolRegistry

TASK_TOOLS = {"task_create", "task_read", "task_update", "task_edit", "task_list", "task_clear"}


def _new_map() -> TaskMap:
    """扬声器例图: n0 -> n2 -> n1"""
    m = TaskMap.create(1, "修扬声器", "扬声器正常出声")
    apply_ops(
        m,
        [
            {"op": "add_node", "desc": "病因已定位"},  # n2
            {"op": "add_edge", "from": "n0", "to": "n2", "semantic": "requires_decompose"},
            {"op": "add_edge", "from": "n2", "to": "n1", "semantic": "complete"},
        ],
    )
    return m


def _lock(m: TaskMap, nid: str, worker: str, did: str) -> None:
    n = m.nodes[nid]
    n.status = NodeStatus.ACTIVE
    n.owner = worker
    n.dispatch_id = did


class TestModels(unittest.TestCase):
    def test_create_basic(self):
        m = TaskMap.create(3, "T", "G")
        assert m.id == 3 and m.goal == "G" and m.next_nid == 2
        assert set(m.nodes) == {"n0", "n1"}
        assert m.nodes["n0"].kind.value == "start"
        assert m.nodes["n1"].kind.value == "goal"
        assert m.edges[0].semantic == "requires_decompose"

    def test_roundtrip(self):
        m = _new_map()
        m.questions.append({"q": "x", "resolved": False})
        d = m.to_dict()
        assert d["edges"][0]["from"] == "n0"  # JSON 键是 from/to
        m2 = TaskMap.from_dict(d)
        assert set(m2.nodes) == set(m.nodes) and len(m2.edges) == len(m.edges)
        assert m2.open_questions()[0]["q"] == "x"

    def test_from_legacy_status(self):
        m = TaskMap.from_legacy({"id": 5, "subject": "S", "status": "in_progress"})
        assert m.id == 5 and m.status == MapStatus.ACTIVE and m.goal == "S"
        m2 = TaskMap.from_legacy({"id": 6, "subject": "S", "description": "D", "status": "completed"})
        assert m2.status == MapStatus.COMPLETED and m2.goal == "D"


class TestStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.fp = os.path.join(self.tmp, ".fp", "tasks.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_create_get(self):
        s = TaskMapStore(self.fp)
        m = s.create("A", "GA")
        assert s.get(m.id).title == "A"
        assert s.get(str(m.id)).id == m.id  # 类型宽容
        assert s.get(float(m.id)).id == m.id
        assert s.get(999) is None

    def test_atomic_no_leftover(self):
        s = TaskMapStore(self.fp)
        s.create("A")
        leftovers = [f for f in os.listdir(os.path.dirname(self.fp)) if f.endswith(".tmp")]
        assert not leftovers

    def test_v2_format(self):
        s = TaskMapStore(self.fp)
        s.create("A")
        with open(self.fp, encoding="utf-8") as f:
            d = json.load(f)
        assert d["version"] == 2 and "maps" in d and "tasks" not in d

    def test_legacy_migration(self):
        os.makedirs(os.path.dirname(self.fp))
        with open(self.fp, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "tasks": [
                        {"id": 7, "subject": "A", "status": "in_progress"},
                        {"id": 8, "subject": "B", "description": "DB", "status": "completed"},
                    ],
                    "next_id": 9,
                },
                f,
            )
        maps, nid = TaskMapStore(self.fp).load()
        assert nid == 9 and len(maps) == 2
        assert maps[0].status == MapStatus.ACTIVE and maps[1].goal == "DB"

    def test_corrupt_backup(self):
        os.makedirs(os.path.dirname(self.fp))
        with open(self.fp, "w", encoding="utf-8") as f:
            f.write("{ not json")
        assert TaskMapStore(self.fp).load() == ([], 1)
        assert os.path.exists(self.fp + ".bak")

    def test_save_map_and_clear(self):
        s = TaskMapStore(self.fp)
        a = s.create("A")
        b = s.create("B")
        a.status = MapStatus.DELIVERED
        s.save_map(a)
        assert s.get(a.id).status == MapStatus.DELIVERED
        b.status = MapStatus.COMPLETED
        s.save_map(b)
        assert s.clear_terminal() == 1  # 只清 completed, delivered 保留
        assert s.get(a.id) is not None and s.get(b.id) is None


class TestGraphOps(unittest.TestCase):
    def test_add_node_edge(self):
        m = TaskMap.create(1, "T")
        summ = apply_ops(
            m,
            [
                {"op": "add_node", "desc": "X"},
                {"op": "add_edge", "from": "n1", "to": "n2", "semantic": "complete"},
                {"op": "set_status", "node": "n2", "status": "active"},
                {"op": "set_evidence", "node": "n2", "evidence": ["e1", "e2"]},
            ],
        )
        assert m.nodes["n2"].status == NodeStatus.ACTIVE and len(m.nodes["n2"].evidence) == 2
        assert len(summ) == 4

    def test_atomic_rollback(self):
        m = _new_map()
        before = (set(m.nodes), len(m.edges), m.next_nid)
        with self.assertRaises(GraphOpError):
            apply_ops(
                m,
                [
                    {"op": "add_node", "desc": "临时"},
                    {"op": "add_edge", "from": "ghost", "to": "n1", "semantic": "x"},
                ],
            )
        assert (set(m.nodes), len(m.edges), m.next_nid) == before

    def test_invalid_op_word(self):
        with self.assertRaises(GraphOpError):
            apply_ops(TaskMap.create(1, "T"), [{"op": "remove_node", "node": "n1"}])
        with self.assertRaises(GraphOpError):
            apply_ops(TaskMap.create(1, "T"), [])


class TestDelta(unittest.TestCase):
    def test_extract_variants(self):
        ok = '正文\n[taskmap-delta]\n{"node":"n1"}\n[/taskmap-delta]'
        d, note = delta_mod.extract_delta(ok)
        assert d == {"node": "n1"} and note == ""
        assert delta_mod.extract_delta("无")[1] == "无 delta 块"
        assert delta_mod.extract_delta("[taskmap-delta]{bad json}[/taskmap-delta]")[0] is None
        # 围栏容忍
        fenced = '```json\n[taskmap-delta]\n{"node":"n1"}\n[/taskmap-delta]\n```'
        assert delta_mod.extract_delta(fenced)[0] == {"node": "n1"}

    def test_apply_ok_and_release_lock(self):
        m = _new_map()
        _lock(m, "n2", "w1", "d1")
        delta = {
            "dispatch_id": "d1",
            "node": "n2",
            "status": "done",
            "outcome": "complete",
            "evidence": ["cmd: x"],
            "propose": {
                "nodes": [{"lid": "z", "desc": "重装驱动"}],
                "edges": [{"from": "n2", "to": "z", "semantic": "complete"}],
            },
        }
        ok, msg, _ = delta_mod.apply_delta(m, delta, worker="w1", dispatch_id="d1")
        assert ok, msg
        assert m.nodes["n2"].status == NodeStatus.DONE
        assert m.nodes["n2"].owner is None and m.nodes["n2"].dispatch_id is None
        assert any(n.desc == "重装驱动" for n in m.nodes.values())

    def test_stale_dispatch_rejected(self):
        m = _new_map()
        _lock(m, "n2", "w1", "d1")
        ok, msg, _ = delta_mod.apply_delta(
            m, {"node": "n2", "status": "done", "outcome": "complete"}, worker="w1", dispatch_id="d_OLD"
        )
        assert not ok and "陈旧" in msg

    def test_bad_outcome(self):
        m = _new_map()
        _lock(m, "n2", "w1", "d1")
        ok, msg, _ = delta_mod.apply_delta(
            m, {"node": "n2", "status": "done", "outcome": "nonsense"}, worker="w1", dispatch_id="d1"
        )
        assert not ok and "outcome" in msg

    def test_out_of_scope_edge(self):
        m = _new_map()
        _lock(m, "n2", "w1", "d1")
        n_before = set(m.nodes)
        delta = {
            "node": "n2",
            "status": "done",
            "outcome": "complete",
            "propose": {
                "nodes": [{"lid": "y", "desc": "不应落地"}],
                "edges": [{"from": "n2", "to": "n0", "semantic": "complete"}],
            },
        }
        ok, msg, _ = delta_mod.apply_delta(m, delta, worker="w1", dispatch_id="d1")
        assert not ok and "越界" in msg
        assert set(m.nodes) == n_before  # propose 原子回滚

    def test_dedup_existing_node(self):
        m = _new_map()
        m.nodes["n3"] = None  # 占位, 下面覆盖
        apply_ops(m, [{"op": "add_node", "desc": "重装驱动"}])
        _lock(m, "n2", "w1", "d1")
        n_before = set(m.nodes)
        delta = {
            "node": "n2",
            "status": "done",
            "outcome": "complete",
            "propose": {
                "nodes": [{"lid": "z", "desc": "重装驱动"}],
                "edges": [{"from": "n2", "to": "z", "semantic": "complete"}],
            },
        }
        ok, _, _ = delta_mod.apply_delta(m, delta, worker="w1", dispatch_id="d1")
        assert ok and set(m.nodes) == n_before

    def test_done_requires_outcome(self):
        m = _new_map()
        ok, msg, _ = delta_mod.apply_delta(m, {"node": "n2", "status": "done"}, worker="")
        assert not ok and "outcome" in msg

    def test_delta_instruction_has_ids(self):
        s = delta_mod.delta_instruction("n7", "d-xyz")
        assert "n7" in s and "d-xyz" in s and delta_mod.DELTA_START in s


class TestRender(unittest.TestCase):
    def test_full_and_subgraph(self):
        m = _new_map()
        full = render.render_full(m)
        assert "修扬声器" in full and "n2" in full
        sub = render.render_subgraph(m, "n2", radius=1)
        assert "n2" in sub and "n1" in sub  # 目标节点恒在
        assert render.EDGE_LEGEND.split(":")[0] in sub

    def test_summary(self):
        m = _new_map()
        s = render.summary([m])
        assert s and "#1" in s and "待办" in s
        m.status = MapStatus.COMPLETED
        assert render.summary([m]) is None


class TestTools(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="fp_test_task_")
        os.chdir(self.tmp)
        os.makedirs(".fp", exist_ok=True)

    def tearDown(self):
        os.chdir(self._orig)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_create_read_list(self):
        assert "已创建任务图 #1" in await tools.handle_create({"title": "A", "goal": "G"})
        assert "A" in await tools.handle_read({"id": 1})
        assert "任务列表" in await tools.handle_list({})
        assert await tools.handle_list({}) != "暂无任务"

    async def test_edit_and_update(self):
        await tools.handle_create({"title": "A"})
        r = await tools.handle_edit({"id": 1, "ops": [{"op": "add_node", "desc": "X"}]})
        assert "图变更已应用" in r and "+节点 n2" in r
        r = await tools.handle_update({"id": 1, "status": "delivered", "add_question": "q?"})
        assert "+question" in r
        assert "待澄清" in await tools.handle_read({"id": 1})

    async def test_edit_rejected_atomic(self):
        await tools.handle_create({"title": "A"})
        r = await tools.handle_edit({
            "id": 1,
            "ops": [{"op": "add_edge", "from": "n0", "to": "ghost", "semantic": "x"}],
        })
        assert "被拒" in r
        assert "n2" not in await tools.handle_read({"id": 1})

    async def test_update_resolve_and_bad(self):
        await tools.handle_create({"title": "A"})
        await tools.handle_update({"id": 1, "add_question": "q0"})
        assert "resolve#0" in await tools.handle_update({"id": 1, "resolve_question": 0})
        with self.assertRaises(ValueError):
            await tools.handle_update({"id": 1, "status": "bogus"})
        with self.assertRaises(ValueError):
            await tools.handle_update({"id": 1, "resolve_question": 5})

    async def test_read_missing_and_clear(self):
        assert "未找到" in await tools.handle_read({"id": 42})
        assert "没有终态任务" in await tools.handle_clear({})
        await tools.handle_create({"title": "A"})
        await tools.handle_update({"id": 1, "status": "completed"})
        assert "已清除 1 个" in await tools.handle_clear({})


class TestPlugin(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="fp_test_taskplugin_")
        os.chdir(self.tmp)
        os.makedirs(".fp", exist_ok=True)

    def tearDown(self):
        os.chdir(self._orig)
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _mock_registry() -> ToolRegistry:
        r = ToolRegistry()
        r._plugins.clear()
        return r

    def test_instantiation(self):
        p = TaskSystemPlugin()
        assert p.name == "task_system" and p.is_enabled

    def test_scan_discovery(self):
        lc = LifecycleManager()
        pdir = os.path.join(os.path.dirname(__file__), "..", "src", "fp_core", "plugins")
        reg = PluginRegistry(lc, plugin_dir=pdir)
        assert "task_system" in reg.list_plugins()

    async def test_on_init_and_unregister(self):
        lc = LifecycleManager()
        p = TaskSystemPlugin()
        p.on_register(lc)
        reg = self._mock_registry()
        ctx = await lc.emit(LifecycleHook.ON_INIT, tool_registry=reg)
        names = {d["function"]["name"] for d in reg.get_all_definitions()}
        assert names >= TASK_TOOLS
        ap = ctx.data.get("system_prompt_append")
        assert isinstance(ap, list) and any("任务图" in s for s in ap)
        p.on_unregister()
        after = {d["function"]["name"] for d in reg.get_all_definitions()}
        assert not (TASK_TOOLS & after)

    async def test_before_llm_call_hint(self):
        lc = LifecycleManager()
        p = TaskSystemPlugin()
        p.on_register(lc)
        await lc.emit(LifecycleHook.ON_INIT, tool_registry=None)
        TaskMapStore().create("演示")
        ctx = await lc.emit(LifecycleHook.ON_BEFORE_LLM_CALL, messages=[{"role": "system", "content": "S"}], tools=[])
        mod = ctx.data.get("modified_messages")
        assert mod and "[task] ▶#1 演示" in mod[-1]["content"]

    async def test_before_llm_call_no_tasks(self):
        lc = LifecycleManager()
        p = TaskSystemPlugin()
        p.on_register(lc)
        await lc.emit(LifecycleHook.ON_INIT, tool_registry=None)
        ctx = await lc.emit(LifecycleHook.ON_BEFORE_LLM_CALL, messages=[{"role": "system", "content": "S"}], tools=[])
        assert ctx.data.get("modified_messages") is None


if __name__ == "__main__":
    unittest.main()
