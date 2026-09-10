"""agent_orchestrator(编排器)插件测试

覆盖: 插件注册/自禁/卸载 / _execute 参数校验 / 与任务图的集成(单写者落图) /
无 delta 的状态兜底 / runs 产物归档 / scheduler DAG。
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import fp_core.plugins.agent_orchestrator.plugin as plugin_mod
from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_core.plugins.agent_orchestrator import OrchestratorPlugin
from fp_core.plugins.agent_orchestrator.fp_multiagent import runs, scheduler
from fp_core.plugins.base.plugin import PluginRegistry
from fp_core.taskmap.graph import apply_ops
from fp_core.taskmap.models import NodeStatus
from fp_core.taskmap.store import TaskMapStore
from fp_core.tools import ToolRegistry


def _fake_spawn(payload: dict, sink: dict | None = None):
    """返回一个假的 spawn_worker: 把 payload 包成 stdout。"""

    async def _f(**kw):
        if sink is not None:
            sink["context"] = kw.get("context", "")
            sink["cwd"] = kw.get("cwd", "")
        import re

        m = re.search(r'"dispatch_id":"([^"]+)"', kw.get("context", ""))
        delta = dict(payload)
        if m:
            delta.setdefault("dispatch_id", m.group(1))
        body = "正文输出...\n[taskmap-delta]\n" + json.dumps(delta) + "\n[/taskmap-delta]"
        return {"status": "ok", "result": body, "error": "", "duration": 0.01, "sid": "s_fake"}

    return _f


async def _plain_spawn(**kw):
    return {"status": "ok", "result": "纯文本输出", "error": "", "duration": 0.01, "sid": "s_plain"}


class TestPluginLifecycle(unittest.IsolatedAsyncioTestCase):
    def test_instantiation(self):
        p = OrchestratorPlugin()
        assert p.name == "agent_orchestrator" and p.is_enabled

    def test_scan_discovery(self):
        lc = LifecycleManager()
        pdir = os.path.join(os.path.dirname(__file__), "..", "src", "fp_core", "plugins")
        reg = PluginRegistry(lc, plugin_dir=pdir)
        assert "agent_orchestrator" in reg.list_plugins()

    def test_self_disable_in_subagent(self):
        lc = LifecycleManager()
        p = OrchestratorPlugin()
        with patch.dict(os.environ, {"FP_IS_SUBAGENT": "1"}):
            p.on_register(lc)
        assert not p.is_enabled
        # 未注册任何钩子
        assert lc.get_hooks() == []

    async def test_on_init_and_unregister(self):
        lc = LifecycleManager()
        p = OrchestratorPlugin()
        p.on_register(lc)
        reg = ToolRegistry()
        reg._plugins.clear()
        ctx = await lc.emit(LifecycleHook.ON_INIT, tool_registry=reg)
        assert "agent_dispatch" in {d["function"]["name"] for d in reg.get_all_definitions()}
        assert any("agent_dispatch" in s for s in ctx.data.get("system_prompt_append", []))
        p.on_unregister()
        assert "agent_dispatch" not in {d["function"]["name"] for d in reg.get_all_definitions()}


class TestExecuteValidation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = OrchestratorPlugin()
        self.p.on_register(LifecycleManager())

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_bad_cwd(self):
        out = json.loads(await self.p._execute({"cwd": "/no/such/dir", "tasks": [{"id": "a", "task": "t"}]}))
        assert out["status"] == "error" and "cwd" in out["result"]

    async def test_empty_tasks(self):
        out = json.loads(await self.p._execute({"cwd": self.tmp, "tasks": []}))
        assert out["status"] == "error"

    async def test_duplicate_ids(self):
        out = json.loads(
            await self.p._execute({"cwd": self.tmp, "tasks": [{"id": "a", "task": "1"}, {"id": "a", "task": "2"}]})
        )
        assert out["status"] == "error" and "重复" in out["result"]

    async def test_node_id_without_task_id(self):
        out = json.loads(await self.p._execute({"cwd": self.tmp, "tasks": [{"id": "a", "task": "t", "node_id": "n1"}]}))
        assert out["status"] == "error" and "task_id" in out["result"]

    async def test_node_id_not_in_map(self):
        with patch.object(plugin_mod, "spawn_worker", _plain_spawn):
            out = json.loads(
                await self.p._execute({
                    "cwd": self.tmp,
                    "task_id": 1,
                    "tasks": [{"id": "a", "task": "t", "node_id": "n1"}],
                })
            )
        assert out["status"] == "error" and "未找到任务图" in out["result"]


class TestDispatchPlain(unittest.IsolatedAsyncioTestCase):
    """无 task_id: 保持原有行为(向后兼容)"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.p = OrchestratorPlugin()
        self.p.on_register(LifecycleManager())
        self._rp = patch.object(runs, "RUNS_ROOT", os.path.join(self.tmp, "runs"))
        self._rp.start()

    def tearDown(self):
        self._rp.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_digest_no_taskmap(self):
        with patch.object(plugin_mod, "spawn_worker", _plain_spawn):
            out = json.loads(await self.p._execute({"cwd": self.tmp, "tasks": [{"id": "a", "task": "t"}]}))
        assert out["status"] == "ok" and "taskmap" not in out
        assert out["tasks"][0]["state"] == "DONE" and out["summary"]["done"] == 1
        assert os.path.exists(out["runs"])

    async def test_pipeline_injects_context(self):
        seen = {}

        async def cap(**kw):
            seen[kw.get("cwd")] = kw.get("context", "")
            return {"status": "ok", "result": "上游产出X", "error": "", "duration": 0.0, "sid": "s"}

        with patch.object(plugin_mod, "spawn_worker", cap):
            await self.p._execute({
                "cwd": self.tmp,
                "mode": "pipeline",
                "tasks": [{"id": "a", "task": "t1"}, {"id": "b", "task": "t2", "deps": ["a"]}],
            })
        # b 的 context 应含上游产出(经 effective_context)
        assert any("上游产出X" in v for v in seen.values())


class TestTaskmapIntegration(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = os.getcwd()
        self.tmp = tempfile.mkdtemp(prefix="fp_test_orch_")
        os.chdir(self.tmp)
        os.makedirs(".fp", exist_ok=True)
        self._rp = patch.object(runs, "RUNS_ROOT", os.path.join(self.tmp, "runs"))
        self._rp.start()
        self.p = OrchestratorPlugin()
        self.p.on_register(LifecycleManager())
        # 建图 #1: n0 -> n2 -> n1
        s = TaskMapStore()
        m = s.create("修扬声器", "扬声器正常出声")
        apply_ops(
            m,
            [
                {"op": "add_node", "desc": "病因已定位"},
                {"op": "add_edge", "from": "n0", "to": "n2", "semantic": "requires_decompose"},
                {"op": "add_edge", "from": "n2", "to": "n1", "semantic": "complete"},
            ],
        )
        s.save_map(m)

    def tearDown(self):
        self._rp.stop()
        os.chdir(self._orig)
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def test_delta_applied(self):
        sink = {}
        spawn = _fake_spawn(
            {
                "node": "n2",
                "status": "done",
                "outcome": "complete",
                "evidence": ["cmd: x"],
                "propose": {
                    "nodes": [{"lid": "z", "desc": "重装驱动"}],
                    "edges": [{"from": "n2", "to": "z", "semantic": "complete"}],
                },
            },
            sink,
        )
        with patch.object(plugin_mod, "spawn_worker", spawn):
            out = json.loads(
                await self.p._execute({
                    "cwd": self.tmp,
                    "task_id": 1,
                    "tasks": [{"id": "w1", "task": "诊断", "node_id": "n2"}],
                })
            )
        assert out["taskmap"]["applied"], out["taskmap"]
        assert "子图" in sink["context"] and "[taskmap-delta]" in sink["context"]  # 注入了子图+约定
        m = TaskMapStore().get(1)
        assert m.nodes["n2"].status == NodeStatus.DONE
        assert m.nodes["n2"].owner is None
        assert any(n.desc == "重装驱动" for n in m.nodes.values())

    async def test_no_delta_fallback(self):
        with patch.object(plugin_mod, "spawn_worker", _plain_spawn):
            out = json.loads(
                await self.p._execute({
                    "cwd": self.tmp,
                    "task_id": 1,
                    "tasks": [{"id": "w1", "task": "x", "node_id": "n2"}],
                })
            )
        assert out["taskmap"]["applied"], out["taskmap"]
        m = TaskMapStore().get(1)
        assert m.nodes["n2"].status == NodeStatus.DONE  # 兜底置 done
        assert m.nodes["n2"].owner is None

    async def test_rejected_delta_fallback(self):
        # delta 的 node 指向别的节点 → 被拒 → 兜底
        spawn = _fake_spawn({"node": "n1", "status": "done", "outcome": "complete"})
        with patch.object(plugin_mod, "spawn_worker", spawn):
            out = json.loads(
                await self.p._execute({
                    "cwd": self.tmp,
                    "task_id": 1,
                    "tasks": [{"id": "w1", "task": "x", "node_id": "n2"}],
                })
            )
        assert out["taskmap"]["warnings"], out["taskmap"]
        assert TaskMapStore().get(1).nodes["n2"].status == NodeStatus.DONE


class TestRuns(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self._rp = patch.object(runs, "RUNS_ROOT", os.path.join(self.tmp, "runs"))
        self._rp.start()

    def tearDown(self):
        self._rp.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_save_load_upsert_delete(self):
        runs.save_run("r1", {"entries": []})
        assert runs.load_run("r1")["run_id"] == "r1"
        runs.upsert_entry("r1", {"task_id": "a", "state": "RUNNING"})
        runs.upsert_entry("r1", {"task_id": "a", "state": "DONE"})
        entries = runs.load_run("r1")["entries"]
        assert len(entries) == 1 and entries[0]["state"] == "DONE"
        assert "r1" in runs.list_runs()
        runs.delete_run("r1")
        assert runs.load_run("r1") is None


class TestScheduler(unittest.IsolatedAsyncioTestCase):
    async def test_dag_and_fail_fast(self):
        order: list[str] = []

        async def spawn(t):
            order.append(t.id)
            if t.id == "b":
                return {"status": "error", "result": "", "error": "boom", "duration": 0.0, "sid": ""}
            return {"status": "ok", "result": "x", "error": "", "duration": 0.0, "sid": ""}

        g = scheduler.TaskGraph(
            root_id="r",
            tasks=[
                scheduler.Task(id="a", task="A"),
                scheduler.Task(id="b", task="B", deps=["a"]),
                scheduler.Task(id="c", task="C", deps=["b"]),
            ],
            policy=scheduler.RunPolicy(fail_policy="fail_fast", max_concurrency=2),
        )
        await scheduler.run_graph(g, spawn)
        by = {t.id: t.state for t in g.tasks}
        assert by["a"] == scheduler.TaskState.DONE
        assert by["b"] == scheduler.TaskState.FAILED
        assert by["c"] == scheduler.TaskState.SKIPPED


if __name__ == "__main__":
    unittest.main()
